#pragma once

#include <memory>
#include <stdarg.h>

#include <cuda.h>


#include "torch/torch.h"

// #include "hash_sdf/jitify_helper/jitify_options.hpp" //Needs to be added BEFORE jitify because this defined the include paths so that the kernels cna find each other
// #include "jitify/jitify.hpp"
#include <Eigen/Dense>

#ifdef HSDF_WITH_GL
    #include "easy_pbr/Frame.h"
    #include "easy_gl/Shader.h"
    #include "easy_gl/GBuffer.h"
#endif

#include "data_loaders/TensorReel.h"


namespace easy_pbr{
    class Mesh;
    class MeshGL;
    // class Frame;
    class Viewer;
}
namespace radu { namespace utils{
    class RandGenerator;
}}





// class Lattice : public torch::autograd::Variable, public std::enable_shared_from_this<Lattice>{
// class Lattice : public at::Tensor, public std::enable_shared_from_this<Lattice>{
class HashSDF : public std::enable_shared_from_this<HashSDF>{
// class Lattice : public torch::Tensor, public std::enable_shared_from_this<Lattice>{
// class Lattice :public THPVariable, public std::enable_shared_from_this<Lattice>{
public:
    template <class ...Args>
    static std::shared_ptr<HashSDF> create( Args&& ...args ){
        return std::shared_ptr<HashSDF>( new HashSDF(std::forward<Args>(args)...) );
    }
    ~HashSDF();


    
    //static stuff
    static std::tuple<torch::Tensor, torch::Tensor, torch::Tensor, torch::Tensor, torch::Tensor> 
        random_rays_from_reel(const TensorReel& reel, const int nr_rays);
    static std::tuple<torch::Tensor, torch::Tensor, torch::Tensor, torch::Tensor, torch::Tensor> 
        rays_from_reprojection_reel(const TensorReel& reel, const torch::Tensor& points_reprojected);
    static torch::Tensor spherical_harmonics(const torch::Tensor& dirs, const int degree);
    static torch::Tensor update_errors_of_matching_indices(const torch::Tensor& old_indices, const torch::Tensor& old_errors, const torch::Tensor& new_indices, const torch::Tensor& new_errors);
    static torch::Tensor meshgrid3d(const float min, const float max, const int nr_points_per_dim);
    //for sampling with low discrepancy
    // std::vector<unsigned> init_sampler();
    static double phi(const unsigned &i);
    static Eigen::VectorXi low_discrepancy2d_sampling(const int nr_samples, const int height, const int width);

    
    #ifdef HSDF_WITH_GL
        void init_opengl();
        void compile_shaders();
        // std::tuple<torch::Tensor, torch::Tensor, torch::Tensor, torch::Tensor> render_atributes(const std::shared_ptr<easy_pbr::Mesh>& mesh, const easy_pbr::Frame frame);
        torch::Tensor render_atributes(const std::shared_ptr<easy_pbr::Mesh>& mesh, const easy_pbr::Frame frame);

        //render into uvt
        gl::GBuffer m_atrib_gbuffer; //we render into it xyz,dir,uv
        gl::Shader m_atrib_shader; //onyl used to render into the depth map and nothing else
    #endif

    std::shared_ptr<easy_pbr::Viewer> m_view;

    // std::vector< std::shared_ptr<easy_pbr::MeshGL> > m_meshes_gl; //stored the gl meshes which will get updated if the meshes in the scene are dirty

    std::shared_ptr<radu::utils::RandGenerator> m_rand_gen;
    static std::shared_ptr<radu::utils::RandGenerator> m_rand_gen_static;



   

private:
    HashSDF( const std::shared_ptr<easy_pbr::Viewer>& view);

    static std::vector<unsigned char> lutLDBN_BNOT;
    static std::vector<unsigned char> lutLDBN_STEP;
    static std::vector<unsigned> mirror;

  
};

