#!/usr/bin/env python3

#this scripts shows how to run PermutoSDF on your own custom dataset
#You would need to modify the function create_custom_dataset() to suit your needs. The current code is setup to read from the easypbr_render dataset (see README.md for the data) but you need to change it for your own data. The main points are that you need to provide an image, intrinsics and extrinsics for each your cameras. Afterwards you need to scale your scene so that your object of interest lies within the bounding sphere of radius 0.5 at the origin.

#CALL with ./permuto_sdf_py/experiments/run_custom_dataset/run_custom_dataset.py --exp_info test [--no_viewer]

import torch
import argparse
import os
import natsort
import numpy as np

import easypbr
from easypbr  import *
from dataloaders import *

import permuto_sdf
from permuto_sdf  import TrainParams
from permuto_sdf_py.utils.common_utils import create_dataloader
from permuto_sdf_py.utils.permuto_sdf_utils import get_frames_cropped
from permuto_sdf_py.train_permuto_sdf import train
from permuto_sdf_py.train_permuto_sdf import HyperParamsPermutoSDF
import permuto_sdf_py.paths.list_of_training_scenes as list_scenes

import json
import cv2

torch.manual_seed(0)
torch.set_default_tensor_type(torch.cuda.FloatTensor)


parser = argparse.ArgumentParser(description='Train sdf and color')
parser.add_argument('--dataset', default="custom", help='Dataset name which can also be custom in which case the user has to provide their own data')
parser.add_argument('--dataset_path', default="/media/rosu/Data/data/permuto_sdf_data/easy_pbr_renders/head/", help='Dataset path')
parser.add_argument('--with_mask', action='store_true', help="Set this to true in order to train with a mask")
parser.add_argument('--exp_info', default="", help='Experiment info string useful for distinguishing one experiment for another')
parser.add_argument('--no_viewer', action='store_true', help="Set this to true in order disable the viewer")
parser.add_argument('--scene_scale', default=0.25, type=float, help='Scale of the scene so that it fits inside the unit sphere')
parser.add_argument('--scene_translation', default=[0.0861233,0.428559, 0.125612], type=float, nargs=3, help='Translation of the scene so that it fits inside the unit sphere')
parser.add_argument('--img_subsample', default=2., type=float, help="The higher the subsample value, the smaller the images are. Useful for low vram")
parser.add_argument('--img_folder', default="/workspace/permuto_sdf/permuto_sdf_py/data/fuwa/images", type=str, help='')
parser.add_argument('--mask_folder', default="/workspace/permuto_sdf/permuto_sdf_py/data/fuwa/mask", type=str, help='')
parser.add_argument('--img_json', default="/workspace/permuto_sdf/permuto_sdf_py/data/fuwa/images.json", type=str, help='')
parser.add_argument('--coorid', default="cv", type=str, help='cv or gl')
args = parser.parse_args()
with_viewer=not args.no_viewer

#MODIFY these for your dataset!
SCENE_SCALE=args.scene_scale
SCENE_TRANSLATION=args.scene_translation
IMG_SUBSAMPLE_FACTOR=args.img_subsample #subsample the image to lower resolution in case you are running on a low VRAM GPU. The higher this number, the smaller the images
DATASET_PATH=args.dataset_path #point this to wherever you downloaded the easypbr_data (see README.md for download link)
IMG_PATH = args.img_folder
MASK_PATH = args.mask_folder
IMG_JSON = args.img_json

def create_custom_dataset():
    info = json.load(open(IMG_JSON))
    K=np.identity(3)
    K[0][0]=info['fl_x'] #fx
    K[1][1]=info['fl_y'] #fy
    K[0][2]=info['cx'] #cx
    K[1][2]=info['cy'] #cy
    imgs_names_list = info['frames']
    path_imgs = IMG_PATH
    frames=[]
    for idx, img in enumerate(imgs_names_list):
        img_name = img['file_name']
        print("img_name", img_name)
        transform_matrix = np.array(img['transform_matrix'])
        frame=Frame()
        img=Mat(os.path.join(path_imgs, img_name))
        img=img.to_cv32f()
        #get rgb part and possibly the alpha as a mask
        if img.channels()==4:
            img_rgb=img.rgba2rgb()
        else:
            img_rgb=img
        frame.width=img.cols
        frame.height=img.rows
        if args.with_mask:
            img_mask = cv2.imread(os.path.join(MASK_PATH, img_name), cv2.IMREAD_GRAYSCALE)
            if img_mask.shape[0] != img.rows or img_mask.shape[1] != img.cols:
                img_mask = cv2.resize(img_mask, (img.cols, img.rows))
            frame.mask=img_mask
        frame.rgb_32f=img_rgb 
        frame.K=K

        # #extrinsics as a tf_cam_world (transformation that maps from world to camera coordiantes)
        # translation_world_cam=calib_line_split[1:4] #translates from cam to world
        # quaternion_world_cam=calib_line_split[4:8] #rotates from cam to world
        # tf_world_cam=Affine3f()
        # tf_world_cam.set_quat(quaternion_world_cam) #assumes the quaternion is expressed as [qx,qy,qz,qw]
        # tf_world_cam.set_translation(translation_world_cam)
        # tf_cam_world=tf_world_cam.inverse() #here we get the tf_cam_world that we need
        if args.coorid == "gl":
            pass
        elif args.coorid == "cv":
            flip_mat = np.array([
                [1, 0, 0, 0],
                [0, 1, 0, 0],
                [0, 0, 1, 0],
                [0, 0, 0, 1]
		    ])
            transform_matrix = np.matmul(transform_matrix, flip_mat)
            transform_matrix = np.linalg.inv(transform_matrix)
            # transform_matrix[1, :] = -transform_matrix[1, :]
            # transform_matrix[2, :] = -transform_matrix[2, :]
        tf_cam_world = Affine3f()
        tf_cam_world.from_matrix(transform_matrix)
        frame.tf_cam_world = tf_cam_world
        #ALTERNATIVELLY if you have already the extrinsics as a numpy matrix you can use the following line
        # frame.tf_cam_world.from_matrix(YOUR_4x4_TF_CAM_WORLD_NUMPY_MATRIX) 

        #scale scene so that the object of interest is within a sphere at the origin with radius 0.5
        # tf_world_cam_rescaled = frame.tf_cam_world.inverse()
        translation=tf_cam_world.translation().copy()
        translation-=SCENE_TRANSLATION
        translation*=SCENE_SCALE
        frame.tf_cam_world.set_translation(translation)

        #subsample the image to lower resolution in case you are running on a low VRAM GPU
        frame=frame.subsample(IMG_SUBSAMPLE_FACTOR)

        #append to the scene so the frustums are visualized if the viewer is enabled
        frustum_mesh=frame.create_frustum_mesh(scale_multiplier=0.06)
        Scene.show(frustum_mesh, "frustum_mesh_"+str(idx))

        #finish
        frames.append(frame)
    
    return frames



def run():

    config_file="train_permuto_sdf.cfg"
    config_path=os.path.join( os.path.dirname( os.path.realpath(__file__) ) , '../../../config', config_file)
    train_params=TrainParams.create(config_path)
    hyperparams=HyperParamsPermutoSDF()


    #get the checkpoints path which will be at the root of the permuto_sdf package 
    permuto_sdf_root=os.path.dirname(os.path.abspath(permuto_sdf.__file__))
    checkpoint_path=os.path.join(permuto_sdf_root, "checkpoints/custom_dataset")
    os.makedirs(checkpoint_path, exist_ok=True)

    
    train_params.set_with_tensorboard(True)
    train_params.set_save_checkpoint(True)
    print("checkpoint_path",checkpoint_path)
    print("with_viewer", with_viewer)

    experiment_name="custom"
    if args.exp_info:
        experiment_name+="_"+args.exp_info
    print("experiment name",experiment_name)


    #CREATE CUSTOM DATASET---------------------------
    frames=create_custom_dataset() 

    #print the scale of the scene which contains all the cameras.
    print("scene centroid", Scene.get_centroid()) #aproximate center of our scene which consists of all frustum of the cameras
    print("scene scale", Scene.get_scale()) #how big the scene is as a measure betwen the min and max of call cameras positions

    ##VISUALIZE
    # view=Viewer.create()
    # while True:
        # view.update()


    ####train
    tensor_reel=MiscDataFuncs.frames2tensors(frames) #make an tensorreel and get rays from all the images at 
    train(args, config_path, hyperparams, train_params, None, experiment_name, with_viewer, checkpoint_path, tensor_reel, frames_train=frames, hardcoded_cam_init=False)



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
