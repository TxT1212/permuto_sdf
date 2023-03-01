#!/usr/bin/env python3



import torch
import torch.nn.functional as F

import sys
import os
import numpy as np
import time

import easypbr
from easypbr  import *
from dataloaders import *

from hash_sdf  import TrainParams
from hash_sdf  import NGPGui
from hash_sdf  import OccupancyGrid
from hash_sdf  import Sphere
from hash_sdf_py.models.models import SDF
from hash_sdf_py.utils.sdf_utils import sdf_loss
from hash_sdf_py.utils.sdf_utils import sphere_trace
from hash_sdf_py.utils.sdf_utils import filter_unconverged_points
from hash_sdf_py.utils.nerf_utils import create_rays_from_frame
from hash_sdf_py.utils.common_utils import show_points
from hash_sdf_py.utils.common_utils import tex2img
from hash_sdf_py.utils.common_utils import colormap
from hash_sdf_py.utils.aabb import AABB

from hash_sdf_py.callbacks.callback import *
from hash_sdf_py.callbacks.viewer_callback import *
from hash_sdf_py.callbacks.visdom_callback import *
from hash_sdf_py.callbacks.tensorboard_callback import *
from hash_sdf_py.callbacks.state_callback import *
from hash_sdf_py.callbacks.phase import *


config_file="train_hashsdf.cfg"

torch.manual_seed(0)
torch.set_default_tensor_type(torch.cuda.FloatTensor)
config_path=os.path.join( os.path.dirname( os.path.realpath(__file__) ) , '../config', config_file)


# #initialize the parameters used for training
train_params=TrainParams.create(config_path)    



def run_net(args, tensor_reel, nr_rays_to_create, ray_origins, ray_dirs, img_indices, min_dist_between_samples, max_nr_samples_per_ray, model, model_rgb, model_bg, model_colorcal, lattice, lattice_bg, iter_nr_for_anneal, aabb, cos_anneal_ratio, forced_variance, nr_samples_bg,  use_occupancy_grid, occupancy_grid, do_imp_sampling, return_features=False):
    with torch.set_grad_enabled(False):

        #intiialize some things
        pred_rgb_bg=None
        pred_normals=None

        # ray_origins, ray_dirs, gt_selected, gt_mask_selected, img_indices=InstantNGP.random_rays_from_reel(tensor_reel, nr_rays_to_create)
        ray_points_entry, ray_t_entry, ray_points_exit, ray_t_exit, does_ray_intersect_box=aabb.ray_intersection(ray_origins, ray_dirs)

        if use_occupancy_grid:
            ray_samples_packed=occupancy_grid.compute_samples_in_occupied_regions(ray_origins, ray_dirs, ray_t_entry, ray_t_exit, min_dist_between_samples, max_nr_samples_per_ray, model.training)
            ray_samples_packed=ray_samples_packed.get_valid_samples()
            # show_points(ray_samples_packed.samples_pos,"samples_pos")
            

            if ray_samples_packed.samples_pos.shape[0]==0: #if we actualyl have samples for this batch fo rays
                pred_rgb=torch.zeros_like(ray_origins)
                pts=torch.zeros_like(ray_origins)
                sdf=torch.zeros_like(ray_origins)[:,0:1]
                sdf_gradients=torch.zeros_like(ray_origins)
                weights=torch.zeros_like(pts)
                weights_sum=torch.zeros_like(ray_origins)[:,0:1]
                pred_normals=torch.zeros_like(ray_origins)
                pred_depth=torch.zeros_like(ray_origins)[:,0:1]
                nr_samples_per_ray=torch.zeros_like(ray_origins)[:,0:1]
                pred_feat=torch.zeros([ray_origins.shape[0],model.feat_size_out])

                return pred_rgb, pts, sdf, sdf_gradients, weights, weights_sum , None, pred_normals, pred_depth, nr_samples_per_ray, pred_feat

            ####IMPORTANCE sampling
            if ray_samples_packed.samples_pos.shape[0]!=0: #if we actualyl have samples for this batch fo rays
                inv_s_imp_sampling=512
                inv_s_multiplier=1.0
                if do_imp_sampling:
                    TIME_START("imp_sample")
                    # print("ray_samples_packed.ray_fixed_dt", ray_samples_packed.ray_fixed_dt)
                    # print("ray_samples_packed.ray_start_end_idx", ray_samples_packed.ray_start_end_idx)
                    # print("ray_samples_packed.cur_nr_samples", ray_samples_packed.cur_nr_samples)
                    # print("ray_samples_packed.samples_pos",ray_samples_packed.samples_pos.shape)
                    # exit(1)
                    # print("ray_samples_packed.ray_fixed_dt", ray_samples_packed.ray_fixed_dt)
                    sdf_sampled_packed, _, _=model(ray_samples_packed.samples_pos, lattice, iter_nr_for_anneal, use_only_dense_grid=False)
                    ray_samples_packed.set_sdf(sdf_sampled_packed) ##set sdf
                    alpha=VolumeRendering.sdf2alpha(ray_samples_packed, sdf_sampled_packed, inv_s_imp_sampling, True, inv_s_multiplier)
                    # print("alpha before clip",alpha.min(), alpha.max())
                    # print("sdf_sampled_packed ",sdf_sampled_packed.min(), sdf_sampled_packed.max())
                    alpha=alpha.clip(0.0, 1.0)
                    transmittance, bg_transmittance= VolumeRendering.cumprod_alpha2transmittance(ray_samples_packed, 1-alpha + 1e-7)
                    weights = alpha * transmittance
                    # print("alpha",alpha.min(), alpha.max())
                    weights_sum, weight_sum_per_sample=VolumeRendering.sum_over_each_ray(ray_samples_packed, weights)
                    # print("weights min max is ", weights.min(), weights.max())
                    # weight_sum_per_sample[weight_sum_per_sample==0]=1e-6 #prevent nans
                    weight_sum_per_sample=torch.clamp(weight_sum_per_sample, min=1e-6 )
                    weights/=weight_sum_per_sample #prevent nans
                    cdf=VolumeRendering.compute_cdf(ray_samples_packed, weights)
                    # print("cdf min max is ", cdf.min(), cdf.max())
                    # exit(1)
                    ray_samples_packed_imp=VolumeRendering.importance_sample(ray_origins, ray_dirs, ray_samples_packed, cdf, 16, model.training)
                    sdf_sampled_packed_imp, _, _=model(ray_samples_packed_imp.samples_pos, lattice, iter_nr_for_anneal, use_only_dense_grid=False)
                    ray_samples_packed_imp.set_sdf(sdf_sampled_packed_imp) ##set sdf
                    ray_samples_combined=VolumeRendering.combine_uniform_samples_with_imp(ray_origins, ray_dirs, ray_t_exit, ray_samples_packed, ray_samples_packed_imp)
                    ray_samples_packed=ray_samples_combined#swap
                    ray_samples_packed=ray_samples_packed.get_valid_samples() #still need to get the valid ones because we have less samples than allocated
                    ####SECOND ITER
                    inv_s_multiplier=2
                    sdf_sampled_packed=ray_samples_packed.samples_sdf #we already combined them and have the sdf
                    alpha=VolumeRendering.sdf2alpha(ray_samples_packed, sdf_sampled_packed, inv_s_imp_sampling, True, inv_s_multiplier)
                    alpha=alpha.clip(0.0, 1.0)
                    transmittance, bg_transmittance= VolumeRendering.cumprod_alpha2transmittance(ray_samples_packed, 1-alpha + 1e-7)
                    weights = alpha * transmittance
                    weights_sum, weight_sum_per_sample=VolumeRendering.sum_over_each_ray(ray_samples_packed, weights)
                    weight_sum_per_sample=torch.clamp(weight_sum_per_sample, min=1e-6 )
                    weights/=weight_sum_per_sample #prevent nans
                    cdf=VolumeRendering.compute_cdf(ray_samples_packed, weights)
                    ray_samples_packed_imp=VolumeRendering.importance_sample(ray_origins, ray_dirs, ray_samples_packed, cdf, 16, model.training)
                    ray_samples_combined=VolumeRendering.combine_uniform_samples_with_imp(ray_origins, ray_dirs, ray_t_exit, ray_samples_packed, ray_samples_packed_imp)
                    ray_samples_packed=ray_samples_combined#swap
                    ray_samples_packed=ray_samples_packed.get_valid_samples() #still need to get the valid ones because we have less samples than allocated
                    TIME_END("imp_sample")
                    #####FINISH imp sampling
                    # pred_imp_points_list.append(ray_samples_packed_imp.samples_pos)

                    #we NEED to do ti again here because the previous check cna be wrong. It check for how many samples we have but not all samples may be valid. this si due to the fact that a ray can ceate less samples than actually it's allocated. The ray_samples is therefore not compact. This samples after doing a combine_uniform_with_imp is always compact so we can safely do this here
                    #TODO maybe make a function to compact the rays and call it in case we are not doing a combine_uniform_samples_with_imp
                    if ray_samples_packed.samples_pos.shape[0]==0: #if we actualyl have samples for this batch fo rays
                        pred_rgb=torch.zeros_like(ray_origins)
                        pts=torch.zeros_like(ray_origins)
                        sdf=torch.zeros_like(ray_origins)[:,0:1]
                        sdf_gradients=torch.zeros_like(ray_origins)
                        weights=torch.zeros_like(pts)
                        weights_sum=torch.zeros_like(ray_origins)[:,0:1]
                        pred_normals=torch.zeros_like(ray_origins)
                        pred_depth=torch.zeros_like(ray_origins)[:,0:1]
                        nr_samples_per_ray=torch.zeros_like(ray_origins)[:,0:1]
                        pred_feat=torch.zeros([ray_origins.shape[0],model.feat_size_out])

                        return pred_rgb, pts, sdf, sdf_gradients, weights, weights_sum , None, pred_normals, pred_depth, nr_samples_per_ray, pred_feat
                else:
                    ray_samples_packed=VolumeRendering.compact_ray_samples(ray_samples_packed) #if we don't do importance sampling we need to pack the rays so there are no invalid samples. Otherwise the combina_uniform with imp already packs them



        else:
            # #make ray samples
            z_vals, z_vals_imp = model.ray_sampler.get_z_vals(ray_origins, ray_dirs, model, lattice, iter_nr_for_anneal, use_only_dense_grid=False) #nr_rays x nr_samples

            #get mid points
            z_vals_rgb = get_midpoint_of_sections(z_vals) #gets a z value for each midpoint 
            
            ray_samples_rgb = ray_origins[:, None, :] + ray_dirs[:, None, :] * z_vals_rgb[..., :, None]  # n_rays, n_samples, 3
            dirs = ray_dirs[:, None, :].expand(ray_samples_rgb.shape )
            nr_rays=ray_samples_rgb.shape[0]
            nr_samples=ray_samples_rgb.shape[1]


            #new stuff based on neus
            pts = ray_samples_rgb.reshape(-1, 3)
            dirs = dirs.reshape(-1, 3)


        if args.without_mask: #get the zs for the background
            use_contract_3d=True
            if use_occupancy_grid:
                ray_samples_packed_bg= RaySampler.compute_samples_bg(ray_origins, ray_dirs, ray_t_exit, nr_samples_bg, aabb.m_radius, aabb.m_center_tensor, model.training, use_contract_3d)
                # show_points(ray_samples_packed_bg.samples_pos, "samples_bg")
            else:
                z_vals_bg, dummy, ray_samples_bg_4d, ray_samples_bg = model_bg.ray_sampler_bg.get_z_vals_bg(ray_origins, ray_dirs, model_bg, lattice_bg, iter_nr_for_anneal)
                dirs_bg = ray_dirs[:, None, :].expand(ray_samples_bg.shape ).contiguous()


    # TIME_END("rgb_prep")


    if not use_occupancy_grid:
        # #predict sdf
        sdf, sdf_gradients, feat, _=model.get_sdf_and_gradient(pts, lattice, iter_nr_for_anneal, use_only_dense_grid=False)
        # #predict rgb
        rgb_samples, rgb_samples_view_dep = model_rgb(model, feat, sdf_gradients, pts, dirs, lattice, iter_nr_for_anneal, model_colorcal, img_indices, nr_rays=nr_rays_to_create)
        rgb_samples=rgb_samples.view(nr_rays, -1, 3)
        #volume render
        weights, inv_s, inv_s_before_exp, bg_transmittance = model.volume_renderer(pts, lattice, z_vals, ray_t_exit, sdf, sdf_gradients, dirs, nr_rays, nr_samples, cos_anneal_ratio, forced_variance=forced_variance) #neus
        pred_rgb = torch.sum(weights.unsqueeze(-1) * rgb_samples, 1)
        weights_sum=torch.sum(weights.unsqueeze(-1) , 1)

        nr_samples_per_ray=torch.ones([nr_rays,1])*nr_samples


        pred_normals= torch.sum(weights.unsqueeze(-1) * sdf_gradients.view(nr_rays_to_create,-1,3), 1)
        pred_depth=  torch.sum(weights * z_vals, 1, keepdim=True)
        pred_feat= torch.sum(weights.unsqueeze(-1) * feat.view(nr_rays_to_create,-1,32), 1)

    else:
        ###FUSED sdf--------------------
        sdf, sdf_gradients, feat, _ =model.get_sdf_and_gradient(ray_samples_packed.samples_pos, lattice, iter_nr_for_anneal, use_only_dense_grid=False)
        # sdf.register_hook(lambda grad: print("HOOK sdf grad min max is ", grad.min(), grad.max()))
        # sdf_gradients.register_hook(lambda grad: print("HOOK sdf_gradients grad min max is ", grad.min(), grad.max()))
        # feat.register_hook(lambda grad: print("HOOK feat grad min max is ", grad.min(), grad.max()))
        #FUSED vol render----------------------
        weights, weights_sum, inv_s, inv_s_before_exp, bg_transmittance = model.volume_renderer.vol_render_samples_packed(ray_samples_packed, ray_t_exit, True, sdf, sdf_gradients, cos_anneal_ratio, forced_variance=forced_variance) #neus
        #Fused RGB---------------------------
        rgb_samples, _ = model_rgb(model, feat, sdf_gradients, ray_samples_packed.samples_pos, ray_samples_packed.samples_dirs, lattice, iter_nr_for_anneal, model_colorcal, img_indices, ray_samples_packed.ray_start_end_idx)
        #FUSED integrate weigths and rgb_samples_fused
        pred_rgb=model.volume_renderer.integrator_module(ray_samples_packed, rgb_samples, weights)
        #pts for later usign them for curvature
        pts=ray_samples_packed.samples_pos

        #get also the normals
        pred_normals=model.volume_renderer.integrator_module(ray_samples_packed, sdf_gradients, weights)
        # pred_normals=torch.nn.functional.normalize(pred_normals,dim=-1)

        #get the depth
        depth_w=ray_samples_packed.samples_z*weights
        pred_depth, _=VolumeRendering.sum_over_each_ray(ray_samples_packed, depth_w)
        nr_samples_per_ray=ray_samples_packed.ray_start_end_idx[:,1:2]-ray_samples_packed.ray_start_end_idx[:,0:1]

        #get the features
        if return_features:
            feat_w=feat*weights
            pred_feat, _=VolumeRendering.sum_over_each_ray(ray_samples_packed, feat_w)
        else:
            pred_feat=None
        
        
    

    #run nerf bg
    if args.without_mask:
        if not use_occupancy_grid:
            rgb_samples_bg, density_samples_bg=model_bg( ray_samples_bg_4d.view(-1,4), dirs_bg.view(-1,3), lattice_bg, iter_nr_for_anneal, model_colorcal, img_indices, nr_rays=nr_rays_to_create) 
            rgb_samples_bg=rgb_samples_bg.view(nr_rays_to_create, nr_samples_bg,3)
            density_samples_bg=density_samples_bg.view(nr_rays_to_create, nr_samples_bg)
            # #get weights for the integration
            weights_bg, disp_map_bg, acc_map_bg, depth_map_bg, _=model_bg.volume_renderer(density_samples_bg, z_vals_bg, None)
            pred_rgb_bg = torch.sum(weights_bg.unsqueeze(-1) * rgb_samples_bg, 1)
        else:
            rgb_samples_bg, density_samples_bg=model_bg( ray_samples_packed_bg.samples_pos_4d.view(-1,4), ray_samples_packed_bg.samples_dirs.view(-1,3), lattice_bg, iter_nr_for_anneal, model_colorcal, img_indices, nr_rays=nr_rays_to_create) 
            # rgb_samples_bg, density_samples_bg=model_bg( ray_samples_packed_bg.samples_pos.view(-1,3), ray_samples_packed_bg.samples_dirs.view(-1,3), lattice_bg, iter_nr_for_anneal, model_colorcal, img_indices, nr_rays=nr_rays_to_create) 
            rgb_samples_bg=rgb_samples_bg.view(nr_rays_to_create, nr_samples_bg,3)
            density_samples_bg=density_samples_bg.view(nr_rays_to_create, nr_samples_bg)
            # #get weights for the integration
            pred_rgb_bg, pred_depth_bg, _, _= model_bg.volume_renderer_general.volume_render_nerf(ray_samples_packed_bg, rgb_samples_bg.view(-1,3), density_samples_bg.view(-1,1), ray_t_exit, False)
    

        #combine attempt 3 like in https://github.com/lioryariv/volsdf/blob/a974c883eb70af666d8b4374e771d76930c806f3/code/model/network_bg.py#L96
        #do all these additions in linear space (we assume the network learns linear space )
        # pred_rgb_bg=srgb_to_linear(pred_rgb_bg)
        # pred_rgb=srgb_to_linear(pred_rgb)
        pred_rgb_bg = bg_transmittance.view(-1,1) * pred_rgb_bg
        pred_rgb = pred_rgb + pred_rgb_bg
        # pred_rgb_bg=linear_to_srgb(pred_rgb_bg)


    # pred_rgb=linear_to_srgb(pred_rgb)



    return pred_rgb, pts, sdf, sdf_gradients, weights, weights_sum, inv_s, pred_normals, pred_depth, nr_samples_per_ray, pred_feat


def run_net_batched(frame, chunk_size,   args, tensor_reel,  min_dist_between_samples, max_nr_samples_per_ray, model, model_rgb, model_bg, model_colorcal, lattice, lattice_bg, iter_nr_for_anneal, aabb, cos_anneal_ratio, forced_variance, nr_samples_bg,  use_occupancy_grid, occupancy_grid, do_imp_sampling, return_features=False):
    ray_origins_full, ray_dirs_full=model.create_rays(frame, rand_indices=None)
    nr_chunks=math.ceil( ray_origins_full.shape[0]/chunk_size)
    ray_origins_list=torch.chunk(ray_origins_full, nr_chunks)
    ray_dirs_list=torch.chunk(ray_dirs_full, nr_chunks)
    pred_rgb_list=[]
    pred_weights_sum_list=[]
    pred_normals_list=[]
    pred_depth_list=[]
    pred_feat_list=[]
    for i in range(len(ray_origins_list)):
        ray_origins=ray_origins_list[i]
        ray_dirs=ray_dirs_list[i]
        nr_rays_chunk=ray_origins.shape[0]
    
        ray_points_entry, ray_t_entry, ray_points_exit, ray_t_exit, does_ray_intersect_box=aabb.ray_intersection(ray_origins, ray_dirs)

        pred_rgb, pts, sdf, sdf_gradients, weights, weights_sum, inv_s, pred_normals, pred_depth, nr_samples_per_ray, pred_feat=run_net(args, tensor_reel, nr_rays_chunk, ray_origins, ray_dirs, None, min_dist_between_samples, max_nr_samples_per_ray, model, model_rgb, model_bg, model_colorcal, lattice, lattice_bg, iter_nr_for_anneal, aabb,  cos_anneal_ratio, forced_variance, nr_samples_bg, use_occupancy_grid, occupancy_grid, do_imp_sampling, return_features=return_features)


        #accumulat the rgb and weights_sum
        pred_rgb_list.append(pred_rgb.detach())
        pred_weights_sum_list.append(weights_sum.detach())
        pred_normals_list.append(pred_normals.detach())
        pred_depth_list.append(pred_depth.detach())
        if return_features:
            pred_feat_list.append(pred_feat.detach())


    #concat
    pred_rgb=torch.cat(pred_rgb_list,0)
    pred_weights_sum=torch.cat(pred_weights_sum_list,0)
    pred_normals=torch.cat(pred_normals_list,0)
    pred_depth=torch.cat(pred_depth_list,0)
    if return_features:
        pred_features=torch.cat(pred_feat_list,0)

    #reshape in imgs
    # print("pred_rgb is ", pred_rgb.shape)
    # print("pred_normals is ", pred_normals.shape)
    pred_rgb_img=lin2nchw(pred_rgb, frame.height, frame.width)
    pred_weights_sum_img=lin2nchw(pred_weights_sum, frame.height, frame.width)
    pred_normals_img=lin2nchw(pred_normals, frame.height, frame.width)
    pred_depth_img=lin2nchw(pred_depth, frame.height, frame.width)
    if return_features:
        pred_features_img=lin2nchw(pred_features, frame.height, frame.width)
    else:
        pred_features_img=None
    # pred_normals_img=(pred_normals_img+1)*0.5

    # show_points(pts,"pts")

    return pred_rgb_img, pred_weights_sum_img, pred_normals_img, pred_depth_img, pred_features_img

def run_net_sphere_traced_batched(frame, chunk_size,   args, tensor_reel,  min_dist_between_samples, max_nr_samples_per_ray, model, model_rgb, model_bg, model_colorcal, lattice, lattice_bg, iter_nr_for_anneal, aabb, cos_anneal_ratio, forced_variance, nr_samples_bg,  use_occupancy_grid, occupancy_grid, nr_iters_sphere_trace, sphere_trace_agressiveness, sphere_trace_converged_threshold, sphere_trace_push_in_gradient_dir, return_features=False):
    ray_origins_full, ray_dirs_full=model.create_rays(frame, rand_indices=None)
    nr_chunks=math.ceil( ray_origins_full.shape[0]/chunk_size)
    ray_origins_list=torch.chunk(ray_origins_full, nr_chunks)
    ray_dirs_list=torch.chunk(ray_dirs_full, nr_chunks)
    pred_rgb_list=[]
    pred_weights_sum_list=[]
    pred_normals_list=[]
    pred_depth_list=[]
    pred_feat_list=[]
    pts_start_list=[]
    pts_end_list=[]
    for i in range(len(ray_origins_list)):
        # print("i",i)
        ray_origins=ray_origins_list[i]
        ray_dirs=ray_dirs_list[i]
        nr_rays_chunk=ray_origins.shape[0]
    
        ray_points_entry, ray_t_entry, ray_points_exit, ray_t_exit, does_ray_intersect_box=aabb.ray_intersection(ray_origins, ray_dirs)

        ray_samples_packed=occupancy_grid.compute_first_sample_start_of_occupied_regions(ray_origins, ray_dirs, ray_t_entry, ray_t_exit)
        ray_samples_packed=ray_samples_packed.get_valid_samples()

        #run sphere tracing for nr_iters
        if ray_samples_packed.samples_pos.shape[0]==0: #if have no samples it means we are all in empty space
            pts_start=torch.zeros_like(ray_origins)
            pts_end=torch.zeros_like(ray_origins)
            pred_rgb=torch.zeros_like(ray_origins)
            pts=torch.zeros_like(ray_origins)
            sdf=torch.zeros_like(ray_origins)[:,0:1]
            sdf_gradients=torch.zeros_like(ray_origins)
            weights=torch.ones_like(pts)
            weights_sum=torch.zeros_like(ray_origins)[:,0:1]
            pred_normals=torch.zeros_like(ray_origins)
            pred_depth=torch.zeros_like(ray_origins)[:,0:1]
            nr_samples_per_ray=torch.zeros_like(ray_origins)[:,0:1]
            pred_feat=torch.zeros([ray_origins.shape[0],model.feat_size_out])
        else: #run sphere tracing
            pos=ray_samples_packed.samples_pos
            start_occupancy_origins_packed=pos
            #move position slightyl inside the voxel
            voxel_size=1.0/occupancy_grid.get_nr_voxels_per_dim()
            pos=pos+ray_samples_packed.samples_dirs*voxel_size*0.5
            # print("pos",pos.shape)
            # print("ray_origins",ray_origins.shape)
            pts_start=pos
            ray_converged_flag=torch.zeros_like(pos)[:,0:1].bool() #all rays start as unconverged
            pts=pos.clone()
            for s_idx in range(nr_iters_sphere_trace): 
                # print("s_idx", s_idx)
                
                #get the positions that are converged
                select_cur_iter=torch.logical_not(ray_converged_flag)
                pos_unconverged=pts[ select_cur_iter.repeat(1,3) ].view(-1,3)
                dirs_unconverged=ray_samples_packed.samples_dirs[ select_cur_iter.repeat(1,3) ].view(-1,3)
                origins_unconverged=start_occupancy_origins_packed[ select_cur_iter.repeat(1,3) ].view(-1,3)
                if pos_unconverged.shape[0]==0:  #all points are converged
                    break;
                # print('pos_unconverged',pos_unconverged.shape)

                # sdf, feat, _=model(pos, lattice, iter_nr_for_anneal, use_only_dense_grid=False)
                # pos=pos+ray_samples_packed.samples_dirs*sdf*sphere_trace_agressiveness

                sdf, feat, _=model(pos_unconverged, lattice, iter_nr_for_anneal, use_only_dense_grid=False)
                pos_unconverged=pos_unconverged+dirs_unconverged*sdf*sphere_trace_agressiveness

                #get the if points are now converged
                newly_converged_flag=sdf.abs()<sphere_trace_converged_threshold
                # print("newly_converged_flag",newly_converged_flag.shape)
                # print("ray_converged_flag[select_cur_iter]",ray_converged_flag[select_cur_iter].shape)
                ray_converged_flag[select_cur_iter]=torch.logical_or(ray_converged_flag[select_cur_iter], newly_converged_flag.view(-1) )
                ray_converged_flag=ray_converged_flag.view(-1,1)


                #check if the new positions are in unnocupied space and if they are move them towards the next occupied voxel
                pos_unconverged, is_within_grid_bounds=occupancy_grid.advance_sample_to_next_occupied_voxel(dirs_unconverged, pos_unconverged)
                ray_converged_flag[select_cur_iter]=torch.logical_or(ray_converged_flag[select_cur_iter], torch.logical_not(is_within_grid_bounds.view(-1)) )
                ray_converged_flag=ray_converged_flag.view(-1,1)


                



                #update the new points
                pts[select_cur_iter.repeat(1,3)]=pos_unconverged.view(-1)

            # print("finished sphere tracing")
            # pts_end=pos

            sdf, sdf_gradients, feat, _=model.get_sdf_and_gradient(pts, lattice, iter_nr_for_anneal, use_only_dense_grid=False, method="finite_difference")
            sdf_gradients=sdf_gradients.detach()

            if sphere_trace_push_in_gradient_dir!=0:
                #move the points in the gradient direction So that they snap to soem surface
                not_converged_flag=sdf>sphere_trace_converged_threshold
                pts[not_converged_flag.repeat(1,3)]=(pts[not_converged_flag.repeat(1,3)]-sdf_gradients[not_converged_flag.repeat(1,3)]*sphere_trace_push_in_gradient_dir).view(-1)
                sdf, sdf_gradients, feat, _=model.get_sdf_and_gradient(pts, lattice, iter_nr_for_anneal, use_only_dense_grid=False, method="finite_difference")

            # not_converged_flag=sdf>sphere_trace_converged_threshold
            # print("sdf",sdf.shape)

            #remove points outside of the volume
            # dist_from_center=torch.norm(pts, dim=-1)
            # pts_outside_of_sphere=dist_from_center>1.0
            is_in_occupied_space=occupancy_grid.check_occupancy(pts)
            # is_in_empty_space=torch.logical_not(is_in_occupied_space)
            # pts[is_in_empty_space.repeat(1,3)]=0.0
            # pts=pts.view(-1,3)
            # sdf_gradients[is_in_empty_space.repeat(1,3)]=0
            # sdf_gradients=sdf_gradients.view(-1,3)

            ##check that the points are still within bounds of the primitive
            pts_norm=pts.norm(dim=-1, keepdim=True)
            is_in_abb=pts_norm<aabb.m_radius
            is_in_occupied_space=torch.logical_and(is_in_occupied_space.view(-1), is_in_abb.view(-1) )
            is_in_occupied_space=is_in_occupied_space.view(-1,1)



            pts_end=pts

            #get color 
            # pts=pos 
            dirs=ray_samples_packed.samples_dirs
            rgb_samples, rgb_samples_view_dep = model_rgb(model, feat, sdf_gradients, pts, dirs, lattice, iter_nr_for_anneal)
            rgb_samples=rgb_samples.view(-1, 3)
            weights=torch.ones_like(rgb_samples)[:,0:1].view(-1,1)
            weights[torch.logical_not(is_in_occupied_space)]=0.0 #set the samples that are outside of the occupancy grid to zero
            pred_rgb=model.volume_renderer.integrator_module(ray_samples_packed, rgb_samples, weights)
            # pred_rgb=rgb_samples.view(-1,3)
            # print("pred_rgb",pred_rgb.min(), pred_rgb.max())
            # print("pred_rgb",pred_rgb.shape)
            # if(pred_rgb.shape[0]!=ray_origins.shape[0]):
            #     print("ray_origins",ray_origins.shape)
            #     print("pred_rgb",pred_rgb.shape)
            #     print("wtf------")
            #     exit(1)
            

            #some things that are just zero in this case
            # pred_normals=torch.zeros_like(ray_origins)
            pred_normals=VolumeRendering.integrate_rgb_and_weights(ray_samples_packed, sdf_gradients, weights)
            # weights_sum=torch.ones_like(ray_origins)[:,0:1] #they are used for alpha so we set them to 1
            # weights_sum=weights.view(-1,1) #sicne we have onyl one sample per ray, the weights sum can be viewed as weights directly
            weights_sum, weight_sum_per_sample=VolumeRendering.sum_over_each_ray(ray_samples_packed, weights)
            pred_depth=torch.zeros_like(ray_origins)[:,0:1]


        #accumulat the rgb and weights_sum
        pts_start_list.append(pts_start)
        pts_end_list.append(pts_end)
        pred_rgb_list.append(pred_rgb.detach())
        pred_weights_sum_list.append(weights_sum.detach())
        pred_normals_list.append(pred_normals.detach())
        pred_depth_list.append(pred_depth.detach())
        if return_features:
            pred_feat_list.append(pred_feat.detach())


    #concat
    pts_start=torch.cat(pts_start_list,0)
    pts_end=torch.cat(pts_end_list,0)
    pred_rgb=torch.cat(pred_rgb_list,0)
    pred_weights_sum=torch.cat(pred_weights_sum_list,0)
    pred_normals=torch.cat(pred_normals_list,0)
    pred_depth=torch.cat(pred_depth_list,0)
    if return_features:
        pred_features=torch.cat(pred_feat_list,0)

    #reshape in imgs
    # print("pred_rgb is ", pred_rgb.shape)
    # print("pred_normals is ", pred_normals.shape)
    pred_rgb_img=lin2nchw(pred_rgb, frame.height, frame.width)
    pred_weights_sum_img=lin2nchw(pred_weights_sum, frame.height, frame.width)
    pred_normals_img=lin2nchw(pred_normals, frame.height, frame.width)
    pred_depth_img=lin2nchw(pred_depth, frame.height, frame.width)
    if return_features:
        pred_features_img=lin2nchw(pred_features, frame.height, frame.width)
    else:
        pred_features_img=None
    # pred_normals_img=(pred_normals_img+1)*0.5

    # show_points(pts,"pts")

    return pred_rgb_img, pred_weights_sum_img, pred_normals_img, pred_depth_img, pred_features_img, pts_start, pts_end


def train(args, config_path, loader_train, frames_train, experiment_name, dataset_name, with_viewer, with_tensorboard, save_checkpoint, checkpoint_path, tensor_reel, lr, iter_start_curv, lr_milestones, iter_finish_training):


    #train
    if with_viewer:
        view=Viewer.create(config_path)
        ngp_gui=NGPGui.create(view)
    

    first_time=True

    if first_time and with_viewer:
        view.m_camera.from_string("-1.69051 0.499783 0.824015  -0.119454    -0.5118 -0.0721315 0.847683  0.0532509 -0.0668205 -0.0933657 60 0.0502494 5024.94")
        # zoomint to eh base of the bear to see the error
        # view.m_camera.from_string(" -0.380117 -0.0556938   0.167511  -0.105197  -0.426046 -0.0499672 0.897171  0.116958 -0.208198 -0.237689 60 0.0502494 5024.94")

    
    #params
    nr_lattice_features=2
    nr_resolutions=24
    # nr_resolutions=8
    use_home= args.type=="home"
    use_mask_loss=not args.without_mask
    nr_samples_bg=32
    print("use_all_imgs", args.use_all_imgs)
    print("without_mask", args.without_mask)


    #torch stuff 
    lattice=Lattice.create(config_path, "lattice")
    nr_lattice_vertices=lattice.capacity()
    #make a lattice for 4D nerf for hte background
    lattice_bg=Lattice.create(config_path, "lattice")
    lattice_bg.set_sigmas_from_string("1.0 4") #the sigma doenst really matter but the 4 matters here
    #loader
    # loader_train, loader_test= create_dataloader(dataset_name, args.scene, config_path, use_home, args.use_all_imgs, args.without_mask)

    aabb, aabb_big = create_bb_for_dataset(dataset_name)
    if with_viewer:
        bb_mesh = create_bb_mesh(aabb) 
        Scene.show(bb_mesh,"bb_mesh")


    nr_rays_to_create = define_nr_rays(loader_train, use_home) 
    expected_samples_per_ray=64+16+16 #we have 64 uniform samples and we run two importance samples
    target_nr_of_samples=512*expected_samples_per_ray


    cb=create_callbacks_simpler_1(with_viewer, with_tensorboard, experiment_name, config_path)


    #create phases
    phases= [
        Phase('train', loader_train, grad=True),
    ]
    #model 
    model=SDF(nr_lattice_vertices, nr_lattice_features, nr_resolutions=nr_resolutions, boundary_primitive=aabb_big, feat_size_out=32, nr_iters_for_c2f=10000, N_initial_samples=64, N_samples_importance=64, N_iters_upsample=4 ).to("cuda")
    model_rgb=RGB(nr_lattice_vertices, nr_lattice_features, nr_resolutions=24, feat_size_in=32, nr_cams=loader_train.nr_samples() ).to("cuda")
    model_bg=NerfHash(4, nr_lattice_vertices, nr_lattice_features, nr_resolutions=nr_resolutions, boundary_primitive=aabb, nr_samples_per_ray=nr_samples_bg, nr_iters_for_c2f=1 ).to("cuda")
    model_colorcal=Colorcal(loader_train.nr_samples(), 0)
    occupancy_grid=OccupancyGrid(256, 1.0, [0,0,0])
    # occupancy_grid=OccupancyGrid(128, 1.0, [0,0,0])
    use_occupancy_grid=True
    use_curvature_normal_based=True
    do_imp_sampling=True
    use_colorcal=True
    if not use_colorcal:
        model_colorcal=None





    first_time_getting_control=True
    run_test=True

    iter_finish_curv=iter_start_curv+1001
    forced_variance_finish_iter=35000
    min_dist_between_samples=0.0001
    max_nr_samples_per_ray=64
    # lipshitz_w=1e-5


    is_in_training_loop=True

    #for reprojection
    nr_samples_per_ray_last_iter=None


    while is_in_training_loop:

        for phase in phases:
            cb.epoch_started(phase=phase)
            cb.phase_started(phase=phase)

            while ( phase.samples_processed_this_epoch< phase.loader.nr_samples() ): #we assume we have the data
                if True: 
                    is_training = phase.grad
                    model.train(phase.grad)
                    model_rgb.train(phase.grad)
                    model_bg.train(phase.grad)


                    # #forward
                    with torch.set_grad_enabled(is_training):
                        cb.before_forward_pass() #sets the appropriate sigma for the lattice

                        # print("\n phase.iter_nr",  phase.iter_nr, "phase grad is ", phase.grad)

                        loss, loss_rgb, loss_eikonal, loss_curvature=init_losses() 

                        iter_nr_for_anneal=get_iter_for_anneal(phases[0].iter_nr, args.nr_iter_sphere_fit)
                        in_process_of_sphere_init=phases[0].iter_nr<args.nr_iter_sphere_fit
                        just_finished_sphere_fit=phases[0].iter_nr==args.nr_iter_sphere_fit


                        if in_process_of_sphere_init:
                            loss, loss_sdf, loss_eikonal= loss_sphere_init(dataset_name, 30000, aabb, model, lattice, iter_nr_for_anneal, use_only_dense_grid=False )
                            # exit(1)
                        else:

                            with torch.set_grad_enabled(False):

                                cos_anneal_ratio=map_range_val(iter_nr_for_anneal, 0.0, forced_variance_finish_iter, 0.0, 1.0)
                                forced_variance=map_range_val(iter_nr_for_anneal, 0.0, forced_variance_finish_iter*1, 0.3, 0.8)

                                ray_origins, ray_dirs, gt_selected, gt_mask_selected, img_indices=InstantNGP.random_rays_from_reel(tensor_reel, nr_rays_to_create)
                                
                                nr_rays_cur_iter=nr_rays_to_create

                                #for this pred_depth, get some of the points in 3D, project them in random images and use those to get new rays
                                do_reprojection=False
                                nr_rays_reprojected=128
                                if do_reprojection and nr_samples_per_ray_last_iter is not None:
                                    #get the depths for the rays that have more than 2 samples
                                    # pred_depth_rand_indices= torch.randint(pred_depth.shape[0])
                                    enough_samples=nr_samples_per_ray_last_iter>2#we expect more or less 512 rays to have enough samples
                                    # enough_depth=pred_depth>0.1
                                    # select_mask=torch.logical_and(enough_samples, enough_depth)
                                    select_mask=enough_samples
                                    pred_depth_selected=pred_depth_last_iter[select_mask]
                                    nr_rays_for_reprojection=pred_depth_selected.shape[0]
                                    pred_depth_selected=pred_depth_selected.view(nr_rays_for_reprojection,1)
                                    ray_origins_selected=ray_origins_last_iter[select_mask.repeat(1,3)].view(nr_rays_for_reprojection,3)
                                    ray_dirs_selected=ray_dirs_last_iter[select_mask.repeat(1,3)].view(nr_rays_for_reprojection,3)
                                    #sicne the rays are randomly selected, we can get the first X of them and this gives us a random sample of them
                                    pred_depth_selected=pred_depth_selected[0:nr_rays_reprojected, :]
                                    ray_origins_selected=ray_origins_selected[0:nr_rays_reprojected, :]
                                    ray_dirs_selected=ray_dirs_selected[0:nr_rays_reprojected, :]

                                    #debug get pts
                                    # print("ray_origins_selected",ray_origins_selected.shape)
                                    # print("ray_dirs_selected",ray_dirs_selected.shape)
                                    # print("pred_depth_selected",pred_depth_selected.shape)
                                    points_reprojected=ray_origins_selected + ray_dirs_selected*pred_depth_selected
                                    #debug just make the points a constant
                                    # points_reprojected=points_reprojected*0
                                    # points_reprojected+=0.2
                                    show_points(points_reprojected,"points_reprojected")

                                    ray_origins_new, ray_dirs_new, gt_selected_new, gt_mask_selected_new, img_indices_new =InstantNGP.rays_from_reprojection_reel(tensor_reel, points_reprojected)
                                    # print("ray_origins_new",ray_origins_new)

                                    #debug make a sampler from these rays and check that it passes through the point
                                    # sampler=NerfUniformSampler(aabb,32)
                                    # ray_points_entry_new, ray_t_entry_new, ray_points_exit_new, ray_t_exit_new, _=aabb.ray_intersection(ray_origins_new, ray_dirs_new)
                                    # z_vals_debug, _=sampler.get_z_vals(ray_origins_new, ray_dirs_new, ray_t_exit_new, model, lattice, phase.iter_nr)
                                    # ray_samples = ray_origins_new.unsqueeze(1) + z_vals_debug.unsqueeze(2) * ray_dirs_new.unsqueeze(1)
                                    # show_points(ray_samples,"ray_samples")

                                    # print("ray_origins previous", ray_origins.shape)
                                    # print("ray_origins_new", ray_origins_new.shape)

                                    #concat with the other rays from reel
                                    ray_origins=torch.cat([ray_origins,ray_origins_new],0)
                                    ray_dirs=torch.cat([ray_dirs,ray_dirs_new],0)
                                    gt_selected=torch.cat([gt_selected,gt_selected_new],0)
                                    gt_mask_selected=torch.cat([gt_mask_selected, gt_mask_selected_new],0)
                                    img_indices=torch.cat([img_indices, img_indices_new],0)

                                    # print("ray_origins after", ray_origins.shape)
                                    # print("nr_rays_cur_iter_ vefore",nr_rays_cur_iter)

                                    nr_rays_cur_iter+=nr_rays_reprojected

                                    # print("nr_rays_cur_iter after",nr_rays_cur_iter)



                                ray_points_entry, ray_t_entry, ray_points_exit, ray_t_exit, does_ray_intersect_box=aabb.ray_intersection(ray_origins, ray_dirs)

                            pred_rgb, pts, sdf, sdf_gradients, weights, weights_sum, inv_s, pred_normals, pred_depth, nr_samples_per_ray, pred_features=run_net(args, tensor_reel, nr_rays_cur_iter, ray_origins, ray_dirs, img_indices, min_dist_between_samples, max_nr_samples_per_ray, model, model_rgb, model_bg, model_colorcal, lattice, lattice_bg, iter_nr_for_anneal, aabb,  cos_anneal_ratio, forced_variance, nr_samples_bg, use_occupancy_grid, occupancy_grid, do_imp_sampling)

                            #save some things
                            pred_depth_last_iter=pred_depth.detach()
                            nr_samples_per_ray_last_iter=nr_samples_per_ray.detach()
                            ray_origins_last_iter=ray_origins.detach()
                            ray_dirs_last_iter=ray_dirs.detach()

                            
                            
                            


                            if phase.iter_nr%8==0 and use_occupancy_grid:
                                with torch.set_grad_enabled(False):
                                    grid_centers_random, grid_center_indices=occupancy_grid.compute_random_sample_of_grid_points(256*256*4,True)
                                    sdf_grid,_,_=model( grid_centers_random, lattice, iter_nr_for_anneal, False) 
                                    grad_norm=torch.linalg.norm(sdf_gradients.detach().reshape(-1, 3), ord=2, dim=-1)
                                    max_eikonal_abs= (grad_norm - 1.0).abs().max()
                                    # print("inv_s",inv_s)
                                    occupancy_thresh=1e-4
                                    occupancy_grid.update_with_sdf_random_sample(grid_center_indices, sdf_grid, inv_s.view(-1)[0].item(), max_eikonal_abs, occupancy_thresh )
                                    #adjust nr_rays_to_create based on how many samples we have in total
                                    # cur_nr_samples=ray_samples_packed.samples_pos.shape[0]
                                    cur_nr_samples=pts.shape[0]
                                    multiplier_nr_samples=float(target_nr_of_samples)/cur_nr_samples
                                    nr_rays_to_create=int(nr_rays_to_create*multiplier_nr_samples)
                                    


                            #rgb loss
                            loss_rgb=rgb_loss(gt_selected, pred_rgb, does_ray_intersect_box)
                            loss+=loss_rgb

                            #eikonal loss
                            loss_eikonal =eikonal_loss(sdf_gradients)
                            loss+=loss_eikonal*args.eik_w #10 here and 30 in the curvatures makes the dog looks ncie but loses paws



                            #loss on curvature
                            use_loss_curvature=True
                            loss_curvature=torch.tensor(0)
                            global_weight_curvature=map_range_val(iter_nr_for_anneal, iter_start_curv, iter_finish_curv, 1.0, 0.000) #once we are converged onto good geometry we can safely descrease it's weight so we learn also high frequency detail geometry.
                            if use_loss_curvature and global_weight_curvature>0.0:
                                if not use_curvature_normal_based:
                                    sdf2, sdf_curvature, feat2, _=model.get_sdf_and_curvature_1d_precomputed_gradient( pts, sdf, sdf_gradients, lattice, iter_nr_for_anneal, use_only_dense_grid=False)
                                else:
                                    sdf2, sdf_curvature, feat2=model.get_sdf_and_curvature_1d_precomputed_gradient_normal_based( pts, sdf_gradients, lattice, iter_nr_for_anneal)
                                # sdf_curvature=sdf_curvature.view(nr_rays, -1)
                                
                                # loss_curvature=(sdf_curvature.abs() *weights.view(nr_rays,-1).detach()  ) 
                                # if not use_occupancy_grid:
                                    # loss_curvature=(sdf_curvature.abs().view(-1) *weights.view(-1).detach()  ) 
                                # else:
                                #We also need to multiply with the weights here because we don't want to supervize the curvature of surfaces that are behing the ones that we are actually seeing.
                                # loss_curvature=(sdf_curvature.abs().view(-1) *weights.view(-1).detach()  ) 
                                # loss_curvature=(sdf_curvature.abs().view(-1)   )
                                loss_curvature=(torch.clamp(sdf_curvature,max=0.5).abs().view(-1)   )
                                # loss_curvature=robust_loss_pytorch.general.lossfun(loss_curvature, alpha=torch.tensor(-2.0), scale=torch.tensor(0.1))
                                # loss_curvature=robust_loss_pytorch.general.lossfun( torch.clamp(loss_curvature, min=1e-6), alpha=torch.tensor(-2.0), scale=torch.tensor(0.1))
                                # loss_curvature=loss_curvature*1e3 #we increase the values a bit because afterwards we downweight with the weights and it seems to risk going below 1e-6 so it has numerical issues
                                # loss_curvature=(loss_curvature.view(-1) *weights.view(-1).detach()  ) 
                                # loss_curvature=(sdf_curvature.abs().view(-1)   )  #if we use occupancy and multiply weight weight, we start with low W for the curvature a the surface and increase it later but by then it's already too late to recover from the shitty curvature we have now
                                loss_curvature=loss_curvature.mean()
                                loss+=loss_curvature* args.curv_w*1e-3 *global_weight_curvature #THIS should maybe promote the curvature tangential to the normal because in the direction of the normal it just promotes a constant sdf



                            #highsdf just to avoice voxels becoming "occcupied" due to their sdf dropping to zero
                            offsurface_points=aabb_big.rand_points_inside(nr_points=1024)
                            sdf_rand, _, _=model( offsurface_points, lattice, iter_nr_for_anneal, use_only_dense_grid=False)
                            loss_offsurface_high_sdf=torch.exp(-1e2 * torch.abs(sdf_rand)).mean()
                            loss+=loss_offsurface_high_sdf*1e-3

                            #loss on lipshitz
                            # loss_lipthitz=1
                            # for i in range(len(model_rgb.mlp.layers)):
                            #     loss_lipthitz=loss_lipthitz*torch.nn.functional.softplus(model_rgb.mlp.lipshitz_bound_per_layer[i])
                            # if with_tensorboard:
                            #     cb["tensorboard_callback"].tensorboard_writer.add_scalar('instant_ngp_2/' + phase.name + '/loss_lipthitz', loss_lipthitz.item(), phase.iter_nr)
                            # if iter_nr_for_anneal>=iter_start_curv:
                            #     loss+=loss_lipthitz.mean()*lipshitz_w


                            if use_mask_loss:
                                # weights_sum=torch.sum(weights.unsqueeze(-1) , 1)
                                # print("weights_sum min max is ", weights_sum.min(), weights_sum.max())
                                loss_mask=torch.nn.functional.binary_cross_entropy(weights_sum.clip(1e-3, 1.0 - 1e-3), gt_mask_selected)
                                loss+=loss_mask*0.1 #0.1 works fine for getting the drill bit of the scissors dataset, anything lower is worse



                            if torch.isnan(loss).any():
                                print("detected nan in loss at iter", phase.iter_nr)
                                exit()
                            
                        



                        #if its the first time we do a forward on the model we need to create here the optimizer because only now are all the tensors in the model instantiated
                        # if first_time or just_finished_sphere_fit or iter_nr_for_anneal==iter_start_curv:
                        if first_time or just_finished_sphere_fit:
                        # if first_time:
                            first_time=False

                           

                            params=[]
                            model_params_without_lattice=[]
                            for name, param in model.named_parameters():
                                if "lattice_values" in name:
                                    pass
                                else:
                                    model_params_without_lattice.append(param)
                            model_rgb_params_without_lattice=[]
                            for name, param in model_rgb.named_parameters():
                                if "lattice_values" in name:
                                    pass
                                else:
                                    model_rgb_params_without_lattice.append(param)


                            # params.append( {'params': model.parameters(), 'weight_decay': 1e-6, 'lr': lr, 'name': "model_sdf" } )
                            # params.append( {'params': model.parameters(), 'weight_decay': 0.0, 'lr': lr, 'name': "model_sdf" } )
                            params.append( {'params': model_params_without_lattice, 'weight_decay': 0.0, 'lr': lr, 'name': "model_sdf_without_lattice"} )
                            params.append( {'params': model.lattice_values_monolithic, 'weight_decay': 0.0, 'lr': lr, 'name': "model_sdf_lattice_values"} )

                            
                            # if iter_nr_for_anneal==iter_start_curv:
                            #     params.append( {'params': model_rgb_params_without_lattice, 'weight_decay': 0.1, 'lr': lr, 'name': "model_rgb_without_lattice"} )
                            #     params.append( {'params': model_rgb.lattice_values_monolithic, 'weight_decay': 1.0, 'lr': lr, 'name': "model_rgb_lattice_values"} )
                            # else:
                            params.append( {'params': model_rgb_params_without_lattice, 'weight_decay': 0.0, 'lr': lr, 'name': "model_rgb_without_lattice"} )
                            params.append( {'params': model_rgb.lattice_values_monolithic, 'weight_decay': 0.0, 'lr': lr, 'name': "model_rgb_lattice_values"} )

                            params.append( {'params': model_bg.parameters(), 'weight_decay': 0.0, 'lr': lr, 'name': "model_bg" } )
                            if model_colorcal is not None:
                                params.append( {'params': model_colorcal.parameters(), 'weight_decay': 1e-1, 'lr': lr, 'name': "model_colorcal" } )
                            # optimizer = torch.optim.AdamW (params, amsgrad=False,  betas=(0.9, 0.99), eps=1e-15) #params from instantngp
                            # optimizer = torch.optim.AdamW (params, amsgrad=False)  #no shingles
                            # optimizer = LazyAdam(params, betas=(0.9, 0.99), eps=1e-15) #seems to be a bit faster than adam at the beggining
                            # optimizer = RAdam(params, betas=(0.9, 0.99), eps=1e-15) #makes it slightly "noiser" with more curvature but solves the waviness
                            optimizer=apex.optimizers.FusedAdam(params, adam_w_mode=True, betas=(0.9, 0.99), eps=1e-15)
                            # optimizer=Adan(params, eps=1e-15)
                            # optimizer = RAdam(params) 
                            # optimizer = Adan (params)  #no shingles
                            # optimizer = torch.optim.Adamax (params)
                            # optimizer = torch.optim.Adamax (params, betas=(0.9, 0.99), eps=1e-15)
                            # scheduler = torch.optim.LinearLR(optimizer, start_factor=1.0, end_factor=0.0, total_iters=20000)
                            # scheduler_lr_decay=torch.optim.lr_scheduler.StepLR(optimizer,step_size=5000, gamma=0.1)
                            # scheduler_lr_decay=LinearLR(optimizer, start_factor=1.0, end_factor=0.1, total_iters=100000)
                            # scheduler_lr_decay=LinearLR(optimizer, start_factor=1.0, end_factor=0.1, total_iters=forced_variance_finish_iter*2)
                            # scheduler_lr_decay= MultiStepLR(optimizer, milestones=[40000,80000,100000], gamma=0.3, verbose=False)
                            scheduler_lr_decay= MultiStepLR(optimizer, milestones=lr_milestones, gamma=0.3, verbose=False)
                            # scheduler_lr_decay= torch.optim.lr_scheduler.ExponentialLR(optimizer,)
                            scheduler_warmup=None
                            if just_finished_sphere_fit:
                                scheduler_warmup = GradualWarmupScheduler(optimizer, multiplier=1, total_epoch=3000, after_scheduler=scheduler_lr_decay) #anneal over total_epoch iterations

                            # if load_chkpt:
                                # optimizer.load_state_dict(torch.load(os.path.join(ckpt_path,"optimizer.pt"))  )
                                ##HACK , we know that the worm up has finished so we just set it to None
                                # scheduler_warmup=None
                                

                            scaler = GradScaler()

                        # loss=loss*1e-5

                        cb.after_forward_pass(loss=loss.item(), loss_rgb=loss_rgb, loss_sdf_surface_area=0, loss_sdf_grad=0, phase=phase, loss_eikonal=loss_eikonal.item(), loss_curvature=loss_curvature.item(), neus_variance_mean=model.volume_renderer.deviation_network.get_variance_item(), lr=optimizer.param_groups[0]["lr"]) #visualizes the prediction 
                        # cb.after_forward_pass(loss=loss.item(), loss_rgb=loss_rgb_l1, loss_sdf_surface_area=0, loss_sdf_grad=0, phase=phase, gradient_error=gradient_error, loss_curvature=loss_curvature.item(), neus_variance_mean=0, lr=optimizer.param_groups[0]["lr"]) #visualizes the prediction 
                        # print("lr ", optimizer.param_groups[0]["lr"])

                        #update gui
                        if with_viewer:
                            # prev_c2f_progress=ngp_gui.m_c2f_progress
                            ngp_gui.m_c2f_progress=model.c2f.get_last_t()
                            # print("last t is ", model.c2f.get_last_t())
                            # print("prev_c2f_progress",prev_c2f_progress)
                            # diff=abs(model.c2f.get_last_t()-prev_c2f_progress)
                            # if( diff>0.1 and diff!=0.3 ):
                            #     print("wtf why did we have such a big jump")
                            #     exit(1)



                    #backward
                    if is_training:
                        
                        
                        #at some point we increase the WD for the model_rgb
                        if iter_nr_for_anneal>=iter_start_curv:
                            for group in optimizer.param_groups:
                                if group["name"]=="model_rgb_without_lattice":
                                    #helps to get detail but a very high value causes the model_sdf.lattice values to get higher and higher to compensate. when they get too high the mish activation overflows due to the internal exp
                                    #a value of 0.1 seems to cause overflow
                                    group["weight_decay"]=0.1
                                    # group["weight_decay"]=0.0 #for ablation study
                                if group["name"]=="model_rgb_lattice_values":
                                    group["weight_decay"]=1.0
                                    # group["weight_decay"]=0.0 #for ablation study

                                #decrease eik_w so that we get the wrinkles
                                args.eik_w=0.01
                        #after we optimize with high WD, we lower it again to just get the highlights and sharp view dependant effects
                        if iter_nr_for_anneal>=lr_milestones[1]+5000:
                            for group in optimizer.param_groups:
                                if group["name"]=="model_rgb_without_lattice":
                                    group["weight_decay"]=0.0
                                if group["name"]=="model_rgb_lattice_values":
                                    group["weight_decay"]=0.0
                        # if iter_nr_for_anneal>=(lr_milestones[1]+5000):
                            # lipshitz_w=0 #now that we got good geometry, we can reduce the pressure on the rgb model




                        optimizer.zero_grad()
                        cb.before_backward_pass()
                        TIME_START("backward")
                        # print("------doing loss backward with loss ", loss)
                        loss.backward()
                        # scaler.scale(loss).backward()
                        TIME_END("backward")


                        # summary(model)
                        # summary(model_rgb)
                        # summary(model_bg)
                        # summary(model_colorcal)



                        cb.after_backward_pass()

                        #when going from sphere initialization to normal optimization the loss tends to explode a bit
                        #there is a tendency for the curvature to spike once in a while and this seems to solve it
                        grad_clip=40
                        torch.nn.utils.clip_grad_norm(parameters=model.parameters(), max_norm=grad_clip, norm_type=2.0)
                        torch.nn.utils.clip_grad_norm(parameters=model_rgb.parameters(), max_norm=grad_clip, norm_type=2.0)
                        torch.nn.utils.clip_grad_norm(parameters=model_bg.parameters(), max_norm=grad_clip, norm_type=2.0)
                        if model_colorcal is not None:
                            torch.nn.utils.clip_grad_norm(parameters=model_colorcal.parameters(), max_norm=grad_clip, norm_type=2.0)

                        optimizer.step()
                        # scaler.step(optimizer)
                        # scaler.update()

                        # print("----doing step")

                        if scheduler_warmup is not None:
                            scheduler_warmup.step()


                        if not in_process_of_sphere_init and scheduler_warmup is None: #only doing this if the scheduler warmup doesnt exit. the warmup will call this internally
                            scheduler_lr_decay.step()
                            # print("doing scheduler lr decay phase iter", phase.iter_nr)

                        if phase.iter_nr==iter_finish_training+1:
                            print("Finished training at iter ", phase.iter_nr)
                            is_in_training_loop=False
                            break 

                        # print("finishing here to debug grad")
                        # exit(1)




                with torch.set_grad_enabled(False):
                    model.eval()
                    model_rgb.eval()
                    model_bg.eval()


                    #save checkpoint
                    if (phase.iter_nr%50000==0 or phase.iter_nr==1) and save_checkpoint:
                    # if (phase.iter_nr%1000==0 or phase.iter_nr==1) and save_checkpoint:
                    # if (phase.iter_nr%1000==0 or phase.iter_nr==1) and save_checkpoint:
                    # if (phase.iter_nr%300==0 or phase.iter_nr==1) and save_checkpoint:
                        # root_folder=os.path.join( os.path.dirname(os.path.abspath(__file__))  , "../") #points at the root of hair_recon package
                        root_folder=checkpoint_path
                        print("saving checkpoint at ", checkpoint_path)
                        print("experiment name is",experiment_name)
                        models_path=os.path.join(root_folder,"checkpoints/", experiment_name, str(phase.iter_nr), "models")
                        os.makedirs(models_path, exist_ok=True)
                        model.save(root_folder, experiment_name, phase.iter_nr)
                        model_rgb.save(root_folder, experiment_name, phase.iter_nr)
                        model_bg.save(root_folder, experiment_name, phase.iter_nr)
                        if model_colorcal is not None:
                            model_colorcal.save(root_folder, experiment_name, phase.iter_nr)
                        # torch.save(optimizer.state_dict(), os.path.join(models_path, "optimizer.pt")  )
                        torch.save(torch.get_rng_state(), os.path.join(models_path, "torch_rng_state.pt")  )
                        #save occupancy grid
                        if use_occupancy_grid:
                            torch.save(occupancy_grid.get_grid_values(), os.path.join(models_path, "grid_values.pt")  )
                            torch.save(occupancy_grid.get_grid_occupancy(), os.path.join(models_path, "grid_occupancy.pt"))


                    ###visualize every once in a while
                    should_visualize_things=False
                    if with_viewer:
                        if ngp_gui.m_control_view:
                            should_visualize_things=True
                    # if (phase.iter_nr%500==0 or phase.iter_nr==1 or should_visualize_things or just_finished_sphere_fit) and not in_process_of_sphere_init:
                    if (phase.iter_nr%500==0 or phase.iter_nr==1 or should_visualize_things or just_finished_sphere_fit):

                        if in_process_of_sphere_init:
                            cos_anneal_ratio=1.0
                            forced_variance=1.0

                        print("phase.iter_nr",  phase.iter_nr, "loss ", loss.item() )

                        if model_colorcal is not None:
                            print("model colorcal weight min max",model_colorcal.weight_delta.min(), model_colorcal.weight_delta.max())
                            print("model colorcal bias min max",model_colorcal.bias.min(), model_colorcal.bias.max())
                        # print("model_rgb.lattice_values_monolithic min max ", model_rgb.lattice_values_monolithic.min(), model_rgb.lattice_values_monolithic.max())
                        print("nr_rays_to_create",nr_rays_to_create)
                        # print("model.lattice_values_monolithic min max", model.lattice_values_monolithic.min(), model.lattice_values_monolithic.max())
                        # print("model.feat_scale", model.feat_scale)
                        # print("model_rgb.lattice_values_monolithic min max", model_rgb.lattice_values_monolithic.min(), model_rgb.lattice_values_monolithic.max())

                        #if we have a viewer we visualize there
                        use_only_dense_grid=False
                        if with_viewer:

                            # use_only_dense_grid=ngp_gui.m_use_only_dense_grid
                            # Gui.show(tensor2mat(gt_rgb).rgb2bgr(), "gt_rgb")

                            vis_width=150
                            vis_height=150
                            chunk_size=1000
                            if first_time_getting_control or ngp_gui.m_control_view:
                                first_time_getting_control=False
                                frame_controlable=Frame()
                                frame_controlable.from_camera(view.m_camera, vis_width, vis_height)
                                frustum_mesh_controlable=frame_controlable.create_frustum_mesh(0.1)
                                Scene.show(frustum_mesh_controlable,"frustum_mesh_controlable")


                            frame=frame_controlable
                            #run net
                            pred_rgb_img, pred_weights_sum_img, pred_normals_img, pred_depth_img, pred_features = run_net_batched(frame, chunk_size,   args, tensor_reel, min_dist_between_samples, max_nr_samples_per_ray, model, model_rgb, model_bg, None, lattice, lattice_bg, iter_nr_for_anneal, aabb, cos_anneal_ratio, forced_variance, nr_samples_bg,  use_occupancy_grid, occupancy_grid, do_imp_sampling)

                            #vis
                            Gui.show(tensor2mat(pred_rgb_img).rgb2bgr(), "pred_rgb_img")
                            Gui.show(tensor2mat(pred_weights_sum_img), "pred_weights_sum_img")
                            Gui.show(tensor2mat((pred_normals_img+1)*0.5).rgb2bgr(), "pred_normals_img")
                            Gui.show(tensor2mat(pred_depth_img), "pred_depth_img")

                            if not in_process_of_sphere_init:
                                show_points(pts,"pts")


                            #show a certain layer
                            points_layer, sdf, sdf_gradients= sample_sdf_in_layer(model, lattice, iter_nr_for_anneal, use_only_dense_grid, layer_size=100, layer_y_coord=0)
                            sdf_color=colormap(sdf+0.5, "seismic") #we add 0.5 so as to put the zero of the sdf at the center of the colormap
                            show_points(points_layer, "points_layer", color_per_vert=sdf_color)
                        
                    if (phase.iter_nr%5000==0 or phase.iter_nr==1 or just_finished_sphere_fit) and with_tensorboard and not in_process_of_sphere_init:

                        if isinstance(loader_train, DataLoaderPhenorobCP1):
                            frame=random.choice(frames_train)
                        else:
                            frame=phase.loader.get_random_frame() #we just get this frame so that the tensorboard can render from this frame


                        #make from the gt frame a smaller frame until we reach a certain size
                        frame_subsampled=frame.subsample(2.0, subsample_imgs=False)
                        while min(frame_subsampled.width, frame_subsampled.height) >400:
                            frame_subsampled=frame_subsampled.subsample(2.0, subsample_imgs=False)
                        vis_width=frame_subsampled.width
                        vis_height=frame_subsampled.height
                        frame=frame_subsampled

                        chunk_size=1000


                        pred_rgb_img, pred_weights_sum_img, pred_normals_img, pred_depth_img, pred_features = run_net_batched(frame, chunk_size,   args, tensor_reel, min_dist_between_samples, max_nr_samples_per_ray, model, model_rgb, model_bg, None, lattice, lattice_bg, iter_nr_for_anneal, aabb, cos_anneal_ratio, forced_variance, nr_samples_bg,  use_occupancy_grid, occupancy_grid, do_imp_sampling)

                        #vis
                        cb["tensorboard_callback"].tensorboard_writer.add_image('instant_ngp_2/' + phase.name + '/pred_rgb_img', pred_rgb_img.squeeze(), phase.iter_nr)
                        if pred_normals_img is not None:
                            cb["tensorboard_callback"].tensorboard_writer.add_image('instant_ngp_2/' + phase.name + '/pred_normals_img', torch.clamp(((pred_normals_img+1)*0.5).squeeze(),0,1), phase.iter_nr)
                        
                        #some more scalars
                        # cb["tensorboard_callback"].tensorboard_writer.add_scalar('instant_ngp_2/' + phase.name + '/model_sdf_lattice_max', model.lattice_values_monolithic.max().item(), phase.iter_nr)
                        # cb["tensorboard_callback"].tensorboard_writer.add_scalar('instant_ngp_2/' + phase.name + '/feat_scale', model.feat_scale.item(), phase.iter_nr)


                        





                if phase.loader.is_finished():
                #     cb.epoch_ended(phase=phase, model=model, save_checkpoint=train_params.save_checkpoint(), checkpoint_path=train_params.checkpoint_path() ) 
                #     cb.phase_ended(phase=phase) 
                    phase.loader.reset()


                if with_viewer:
                    view.update()



    print("finished trainng")
    return




def run():

    #argparse
    parser = argparse.ArgumentParser(description='Train sdf and color')
    parser.add_argument('--nr_iter_sphere_fit', type=int, default=4000, help="Nr iterations doing sphere fitting")
    # parser.add_argument('--nr_iter_sphere_fit', type=int, default=4, help="Nr iterations doing sphere fitting")
    parser.add_argument('--eik_w', type=float, default=0.04, help="Weight for the eikonal loss")
    parser.add_argument('--curv_w', type=float, default=1300.0, help="Weight for the curvature loss")
    parser.add_argument('--exp_info', default="", help='Experiment info string useful for distinguishing one experiment for another')
    parser.add_argument('--scene', default="", help='Scene name like dtu_scan24')
    parser.add_argument('--type', required=True, choices=['home', 'remote'], help='Tells which computer are we using')
    parser.add_argument('--use_all_imgs', action='store_true', help="Use all images instead of just the training set")
    parser.add_argument('--without_mask', action='store_true', help="Set this to true in order to train without a mask and model the BG differently")
    args = parser.parse_args()





    experiment_name="s_"+args.exp_info+"_"
    experiment_name+=""+args.scene


    use_home= args.type=="home"

    loader_train, loader_test= create_dataloader(train_params.dataset_name(), args.scene, config_path, use_home, args.use_all_imgs, args.without_mask)
    aabb, aabb_big = create_bb_for_dataset(train_params.dataset_name())

    #crop frames around the plant
    frames_train=None
    if isinstance(loader_train, DataLoaderPhenorobCP1):
        frames_train=get_frames_cropped(loader_train, aabb)
        
    #tensoreel
    if isinstance(loader_train, DataLoaderPhenorobCP1):
        tensor_reel=MiscDataFuncs.frames2tensors(frames_train) #make an tensorreel and get rays from all the images at
    else:
        tensor_reel=MiscDataFuncs.frames2tensors(loader_train.get_all_frames()) #make an tensorreel and get rays from all the images at


    train(args, config_path, loader_train, frames_train, experiment_name, train_params.dataset_name(),  train_params.with_viewer(), train_params.with_tensorboard(), train_params.save_checkpoint(), train_params.checkpoint_path(), tensor_reel, train_params.lr(), 50000 ,[100000,150000,180000,190000], 200000)

    #finished training
    return


  


def main():
    run()



if __name__ == "__main__":
     main()  # This is what you would have, but the following is useful:

    # # These are temporary, for debugging, so meh for programming style.
    # import sys, trace

    # # If there are segfaults, it's a good idea to always use stderr as it
    # # always prints to the screen, so you should get as much output as
    # # possible.
    # sys.stdout = sys.stderr

    # # Now trace execution:
    # tracer = trace.Trace(trace=1, count=0, ignoredirs=["/usr", sys.prefix])
    # tracer.run('main()')