# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved

# DeepFocus source code
# Non-Commercial use only
# Lei Xiao (lei.xiao@fb.com)
# If you use the code, please cite our work "Xiao et al., DeepFocus: Learned Image Synthesis for Computational Displays, ACM SIGGRAPH Asia 2018"

# Mode: varifocal, process each color channel separately

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import time, os, math
import numpy as np
np.random.seed(1)
import tensorflow as tf
tf.set_random_seed(1)
from PIL import Image
import matplotlib.pyplot as plt
from util import *
from preprocess import *

# select GPU
os.environ['CUDA_VISIBLE_DEVICES'] = '0' 

# session mode selection
#MODE = "TRAIN"
MODE = "TEST"

# set true if you wan to load our pretrained model
#RESTORE_TRAINED_MODEL = False
RESTORE_TRAINED_MODEL = True 

# select data
DATA_PATH = '../../../data/'

if RESTORE_TRAINED_MODEL:
   RESTORE_LOGS_DIR = "../../../model/saved_models-lvf/select/" 

if MODE=="TRAIN":
   startSceneIDX = 0 # index of the starting scene
   endSceneIDX = 109 # index of the last scene
   numEvalScenes = 20 # number of scenes among above used for validation during training
else:
   startSceneIDX = 110 # index of the starting scene
   endSceneIDX = 124 # index of the last scene
   numEvalScenes = 0

# save data directories
VERSION = "lvf-new"
TF_SUMMARY_FLAG = "v-%s" % VERSION
if not RESTORE_TRAINED_MODEL:
   RESTORE_LOGS_DIR = "./saved_models-%s/select/" % VERSION 
LOGS_DIR = "./saved_models-%s/" % VERSION
IMGS_DIR = "./results-%s/" % VERSION
PSNR_FN = "./results-%s/test_PSNRs-%s.txt" % (VERSION, VERSION)
SSIM_FN = "./results-%s/test_SSIMs-%s.txt" % (VERSION, VERSION)
LOGS_DIR_SELECT = "%s/select/" % LOGS_DIR
if not os.path.exists(LOGS_DIR):
    os.makedirs(LOGS_DIR)
if not os.path.exists(LOGS_DIR_SELECT):
    os.makedirs(LOGS_DIR_SELECT)
if not os.path.exists(IMGS_DIR):
    os.makedirs(IMGS_DIR)

# option to save images
EXPORT_RESULT_IMAGES = True
EXPORT_8BIT_PNG = False
EXPORT_16BIT_TIFF = True

# hyperparameters
LEARNING_RATE = 1e-4 
BATCH_SIZE = 16 
NUM_EPOCHS = 30 #50
RENORM_SCALE = 1.0
RENORMALIZE_INPUT = True

# interleaving rate
INTERLEAVE_RATE = 2

# training loss
USE_LOG_L1_BOTH = True
USE_LOG_L2_BOTH = False

# decide what data to load
LOAD_RGBD = True
LOAD_FS = True # set True if ground truth defocus images are available

# option to compute quality metrics
REPORT_QUALITY = True

# other parameters
imH, imW = 512, 512 # size of original images
if MODE=="TEST":
   H, W = imH, imW # no need to extract patches for test
else:
   H, W = 128, 128 # size of extracted patches for training

N = 40 # number of images in each input focal stack
film_len_dist = 0.017 # distance between camera film plane and camera lens plane
dp_focal_original = np.linspace(start=0.1, stop=4.0, num=N) # focal distance between 0.1 and 4.0 diopters, with step 0.1 diopters  
fov = 20*np.pi/180 # field of view of the original images
D = 0.004 # pupil diameter of the original images

C = 3 # process each channel separately
depthScale = 12. # divide input depth map by this value # should maintain global relative depth 
diopterScale = 4. # divide input diopter map by this value # should maintain global relative diopter 
cocScale = 30. # divide input COC map by this value # should maintain global relative COC
crop_width = 0 # crop on each side before computing PSNR


#---------------------------------------------------------------------------------------------
# import data
focal_stack, dp_focal, numScenes, numTrainSamples, numEvalSamples, cleanRGB, depth, diopter, validSceneIdxList = \
    loadData(DATA_PATH, \
             N,H,W,imH,imW,dp_focal_original,fov,D,startSceneIDX,endSceneIDX,numEvalScenes,C,depthScale, diopterScale,LOAD_FS, LOAD_RGBD)
   
numTotalSamples = numTrainSamples + numEvalSamples # total number of samples including both for training and for validation during training
print("numTrainSamples=%d, numEvalSamples=%d" % (numTrainSamples, numEvalSamples))

PERIOD = numTrainSamples*N//BATCH_SIZE # number of training steps for each epoch
print("number of period per echo = %d" % PERIOD)
EVAL_STEP = PERIOD // 5
VIS_STEP = 10

#---------------------------------------------------------------------------------------------
# define network
# dimension of input/out data 
INPUT_DIM = 3 #intensity, depth, coc
OUTPUT_DIM = 1 #defocus image intensity

# tf Graph input (only pictures)
X = tf.placeholder("float", [None,INPUT_DIM*(INTERLEAVE_RATE**2),H//INTERLEAVE_RATE,W//INTERLEAVE_RATE])  
Y = tf.placeholder("float", [None,OUTPUT_DIM,H,W])

# layer parameters
weightVarScale = 0.25 
activationFunc = tf.nn.elu
bias_stddev = 0.01

#---------------------------------------------------------------------------------------------
def model(x_in):
    # layer parameters::
    L = 12 # number of layers 
    fwConstant = 3
    fnumConstant = 128 
    fw = np.full((L), fwConstant, dtype=int)  # filter width
    fnum = np.append(np.full((L-1), fnumConstant, dtype=int), OUTPUT_DIM*(INTERLEAVE_RATE**2)) # output channels at each layer

    layers_params = {}
    layers = {}
    prev_layers = {}

    for i in range(0, L):    
        if i==0: # first layer
           in_dim, out_dim = INPUT_DIM*(INTERLEAVE_RATE**2), fnum[i]
        elif i==L-1: # last layer
           in_dim, out_dim = fnum[i-1], OUTPUT_DIM*(INTERLEAVE_RATE**2)
        else:
           in_dim, out_dim = fnum[i-1], fnum[i]  

        layers_params[i] = {'weights':init_weights([fw[i],fw[i],in_dim, out_dim], 'xavier',xavier_params=(in_dim, out_dim),r=weightVarScale),
                     'bias':tf.Variable(tf.truncated_normal([out_dim],stddev=bias_stddev))}

    # build layers::
    print("input data:", x_in.shape)
    if RENORMALIZE_INPUT:
       x_in = (x_in - 0.5*RENORM_SCALE)

    for i in range(0, L):
        if i==0:
            prev_layers[i] = x_in   
        elif (i<3) or (i%2==0): 
            prev_layers[i] = layers[i-1]
        else: 
            prev_layers[i] = layers[i-1] + layers[i-3]
            print('(skip connection: %d, %d)'%(i-1, i-3))
            
        if i==L-1: # last layer
           layers[i] = tf.nn.tanh(tf.layers.batch_normalization(tf.nn.bias_add(tf.nn.conv2d(prev_layers[i],layers_params[i]['weights'],strides=[1,1,1,1], padding='SAME', data_format='NCHW'), layers_params[i]['bias'], data_format='NCHW'),axis=1))    
        else:
           layers[i] = activationFunc(tf.layers.batch_normalization(tf.nn.bias_add(tf.nn.conv2d(prev_layers[i],layers_params[i]['weights'],strides=[1,1,1,1], padding='SAME', data_format='NCHW'), layers_params[i]['bias'], data_format='NCHW'),axis=1))    
        
        print("layer %d:" % i, layers[i].shape)
             
    # renormalize to desired scale    
    x_out = tf.add(0.5, tf.scalar_mul(0.5, layers[L-1]), name='x_out')     
    print("output tensor:", x_out.shape)

    return deinterleave(INTERLEAVE_RATE, x_out)

# construct model
model_op =  model(X)

# target, prediction
labels, predictions = Y, model_op

# shared
rmse_intensity = tf.losses.mean_squared_error(labels=labels, predictions=predictions)
psnr_intensity = 20*log10(RENORM_SCALE) - 10*log10(rmse_intensity)
ssim_intensity = tf.reduce_mean(tf.image.ssim(tf.transpose(labels, [0,2,3,1]), tf.transpose(predictions, [0,2,3,1]), max_val = 1.0))

if USE_LOG_L2_BOTH: 
   labels_dx, labels_dy = calImageGradients(labels)
   preds_dx, preds_dy = calImageGradients(predictions)
   rmse_grad_x, rmse_grad_y = tf.losses.mean_squared_error(labels=labels_dx, predictions=preds_dx), tf.losses.mean_squared_error(labels=labels_dy, predictions=preds_dy)
   psnr_grad_x, psnr_grad_y = -20*log10(RENORM_SCALE) + 10*log10(rmse_grad_x), -20*log10(RENORM_SCALE) + 10*log10(rmse_grad_y)
   loss = -psnr_intensity + 0.5*(psnr_grad_x + psnr_grad_y)

elif USE_LOG_L1_BOTH:
   log_diff_intensity = log10(tf.reduce_mean(tf.abs(labels-predictions))) 
   labels_dx, labels_dy = calImageGradients(labels)
   preds_dx, preds_dy = calImageGradients(predictions)
   log_diff_grad_x = log10(tf.reduce_mean(tf.abs(labels_dx-preds_dx)))
   log_diff_grad_y = log10(tf.reduce_mean(tf.abs(labels_dy-preds_dy)))
   loss = log_diff_intensity + 0.5*(log_diff_grad_x + log_diff_grad_y)   

# training optimizer
optimizer =  tf.train.AdamOptimizer(learning_rate=LEARNING_RATE, beta1=0.9, beta2=0.999, epsilon=1e-8).minimize(loss=loss)
grads_and_vars = tf.train.AdamOptimizer(learning_rate=LEARNING_RATE, beta1=0.9, beta2=0.999, epsilon=1e-8).compute_gradients(loss, tf.trainable_variables()) 

# set up saver
saver = tf.train.Saver(max_to_keep=5)

# initialization
init = tf.global_variables_initializer()

# create a summary to monitor cost tensor
training_summary  = tf.summary.scalar("training loss", loss, family=TF_SUMMARY_FLAG)
validation_summary  = tf.summary.scalar("validation loss", loss, family=TF_SUMMARY_FLAG)

# start training
with tf.Session() as sess:       
    # run the initialization
    sess.run(init)  
    
    # op to write logs to Tensorboard
    summary_writer = tf.summary.FileWriter(LOGS_DIR, graph=tf.get_default_graph())
    
    # restore model if trained 
    if RESTORE_TRAINED_MODEL or MODE!="TRAIN":
        ckpt = tf.train.get_checkpoint_state(RESTORE_LOGS_DIR)
        if ckpt and ckpt.model_checkpoint_path:
            saver.restore(sess, tf.train.latest_checkpoint(RESTORE_LOGS_DIR))
            print("model restored from %s" % RESTORE_LOGS_DIR)
        else:
            input("ERROR: NO RESTORED MODEL...")

    min_ell_eval_mean = []

    # training
    if MODE == "TRAIN":
        print("training mode")
        for e in range(0, NUM_EPOCHS):
            print("--training epoch:", e)

            # generate batch indices
            idxs = np.random.permutation(numTrainSamples*N) # shuffle all samples

            ii = 0
            for i in range(PERIOD):
                # generate current batch
                batch_x = np.zeros((BATCH_SIZE,INPUT_DIM,H,W))
                batch_y = np.zeros((BATCH_SIZE,OUTPUT_DIM,H,W)) 
                for b in range(BATCH_SIZE):
                    idxSample = idxs[ii]//N
                    idxFocal = idxs[ii] - idxSample*N
                    batch_x[b,0,:,:] = cleanRGB[idxSample,:,:].reshape((1,1,H,W))
                    batch_x[b,1,:,:] = diopter[idxSample,:,:].reshape((1,1,H,W))
                    dm = depthScale * depth[idxSample,:,:].reshape((1,1,H,W)) # remember to convert to original un-scaled depth values
                    batch_x[b,2,:,:] = calCoC(D, film_len_dist, dp_focal[idxFocal], fov, H, W, imW, dm, cocScale)                    
                    batch_y[b,0,:,:] = focal_stack[idxSample,:,:,idxFocal].reshape((1,1,H,W))                      
                    ii = ii + 1
                batch_x = interleave_np(INTERLEAVE_RATE,batch_x) #use numpy version
                _, ell, train_summ = sess.run([optimizer, loss, training_summary],feed_dict={X:batch_x, Y:batch_y})

                # Write logs at every iteration
                summary_writer.add_summary(train_summ, e*PERIOD+i)  
              
                # display logs per step
                if (i==1) or (i==PERIOD-1) or (i % VIS_STEP==0):
                   print("training epoch %d, period %d: batch loss (train): %f" % (e, i, ell))

                # validation
                if (i==PERIOD-1) or (i>0 and (i % EVAL_STEP==0)):                
                    # save current model trained 
                    if numEvalSamples==0:
                       saver.save(sess, LOGS_DIR + "model.ckpt", e)                        
                    else: # validation 
                        ell_eval_mean = []
                        ell_psnr_eval_mean = []
                        ell_ssim_eval_mean = []
                        
                        for j in range(0,numEvalSamples):
                            # generate current batch
                            idxs_eval = j + numTrainSamples  # for valiation use, no randomness

                            batch_x_eval = np.zeros((N,INPUT_DIM,H,W))
                            batch_y_eval = np.zeros((N,OUTPUT_DIM,H,W))
                            im = cleanRGB[idxs_eval,:,:].reshape((1,1,H,W))
                            dm = depthScale * depth[idxs_eval,:,:].reshape((1,1,H,W)) # remember to convert to original un-scaled depth values
                            for n in range(N):
                                batch_x_eval[n,0,:,:] = im 
                                batch_x_eval[n,1,:,:] = diopter[idxs_eval,:,:].reshape((1,1,H,W))
                                batch_x_eval[n,2,:,:] = calCoC(D, film_len_dist, dp_focal[n], fov, H, W, imW, dm, cocScale)
                                batch_y_eval[n,0,:,:] = focal_stack[idxs_eval,:,:,n].reshape((1,1,H,W))  
                            batch_x_eval_reshape = interleave_np(INTERLEAVE_RATE, batch_x_eval)
                            
                            # run evauation
                            ell_eval, ell_psnr_eval, ell_ssim_eval = sess.run([loss, psnr_intensity, ssim_intensity], feed_dict={X:batch_x_eval_reshape, Y:batch_y_eval})
                            ell_eval_mean.append(ell_eval)
                            ell_psnr_eval_mean.append(ell_psnr_eval)
                            ell_ssim_eval_mean.append(ell_ssim_eval)

                            
                        validation_summ = tf.Summary()
                        ell_eval_mean = np.mean(ell_eval_mean)                        
                        validation_summ.value.add(tag="validation loss (%s)" % TF_SUMMARY_FLAG, simple_value=ell_eval_mean)
                        print("training epoch %d, period %d, loss (validate): %f" % (e, i, ell_eval_mean))

                        ell_psnr_eval_mean = np.mean(ell_psnr_eval_mean)
                        ell_ssim_eval_mean = np.mean(ell_ssim_eval_mean)
                        validation_summ.value.add(tag="validation psnr intensity (%s)" % TF_SUMMARY_FLAG, simple_value=ell_psnr_eval_mean)
                        validation_summ.value.add(tag="validation ssim intensity (%s)" % TF_SUMMARY_FLAG, simple_value=ell_ssim_eval_mean)
                        print("training epoch %d, period %d, psnr intensity (validate): %f" % (e, i, ell_psnr_eval_mean))
                        print("training epoch %d, period %d, ssim intensity (validate): %f" % (e, i, ell_ssim_eval_mean))
                        
                        # write logs for validation result
                        summary_writer.add_summary(validation_summ, e*PERIOD+i)
                                        
                        if (e==0) or (ell_eval_mean<min_ell_eval_mean):
                            min_ell_eval_mean = ell_eval_mean
                            saver.save(sess, LOGS_DIR_SELECT + "model-best.ckpt", e*PERIOD+i) 
                        #----------------------------------------------------------------------
                    
        print("training is done!")
        print("Run the command line in cmd:\n" \
              "--> tensorboard --logdir=%s " % LOGS_DIR)
        print("The best model is stored at: %s" % LOGS_DIR_SELECT)
    
    # testing
    if MODE == "TEST":    
        print("test mode")
        test_PSNRs = []
        test_SSIMs = []
        recon_fs_rgb = np.zeros((C,H,W,N), dtype=np.float32)
        batch_x = np.zeros((1,INPUT_DIM,H,W), dtype=np.float32)
        batch_y = np.zeros((C,OUTPUT_DIM,H,W,N), dtype=np.float32)
        for i in range(0, numTotalSamples//C, 1):
            v = validSceneIdxList[i]
            for c in range(0, C):
                im = cleanRGB[i*C+c,:,:].reshape((1,1,H,W))
                dm = depthScale * depth[i*C+c,:,:].reshape((1,1,H,W))
                for n in range(0, N):
                    # generate current batch
                    batch_x[0,0,:,:] = im
                    batch_x[0,1,:,:] = diopter[i*C+c,:,:].reshape((1,1,H,W))
                    batch_x[0,2,:,:] = calCoC(D, film_len_dist, dp_focal[n], fov, H, W, imW, dm, cocScale)                    
                    batch_y[c,0,:,:,n] = focal_stack[i*C+c,:,:,n].reshape((1,1,H,W))

                    batch_x_reshape = interleave_np(INTERLEAVE_RATE, batch_x)

                    # run evauation
                    time_start = time.clock()
                    recon = sess.run(model_op, feed_dict={X:batch_x_reshape, Y:batch_y[c,:,:,:,n].reshape((1,OUTPUT_DIM,H,W))})
                    print(time.clock() - time_start)

                    # save
                    recon_fs_rgb[c,:,:,n] = recon.reshape((H,W))
 

            # save image files
            if EXPORT_RESULT_IMAGES:            
                for n in range(0,N,1):
                    im_recon = np.zeros((H,W,C))
                    im_input = np.zeros((H,W,C))
                    for c in range(0,C):
                        im_recon[:,:,c] = np.clip(recon_fs_rgb[c,:,:,n].reshape((H,W)), 0, 1)
                        im_input[:,:,c] = np.clip(batch_y[c,0,:,:,n].reshape((H,W)), 0, 1)
                    
                    if EXPORT_8BIT_PNG:
                       fn = "%s/recon_fs%03d_im%03d.png" % (IMGS_DIR,v,n)                     
                       save_png(im_recon, fn)
                       fn = "%s/gt_fs%03d_im%03d.png" % (IMGS_DIR,v,n)
                       save_png(im_input, fn)
                    elif EXPORT_16BIT_TIFF:
                       fn = "%s/recon_fs%03d_im%03d.tiff" % (IMGS_DIR,v,n)                
                       save_tiff(im_recon, fn)
                       fn = "%s/gt_fs%03d_im%03d.tiff" % (IMGS_DIR,v,n)                
                       save_tiff(im_input, fn)

            # compute and save PSNR
            if REPORT_QUALITY:
               print("calculating PSNR and SSIM...")
               ssims = cal_ssim_focalstack(recon_fs_rgb, batch_y.reshape((C,H,W,N)),crop_width)
               test_SSIMs.append(ssims.eval())
               psnrs = cal_psnr_focalstack(recon_fs_rgb, batch_y.reshape((C,H,W,N)),crop_width)
               test_PSNRs.append(psnrs)
          
        if REPORT_QUALITY:        
            test_PSNRs = np.array(test_PSNRs)
            mean_PSNR_all = np.mean(test_PSNRs)
            mean_PSNR_each = np.mean(test_PSNRs, axis=1)
            myFile = open(PSNR_FN, 'wb')
            np.savetxt(myFile, mean_PSNR_all.reshape((1,1)), fmt='%.4f',header="\nmean PSNR for all test images")
            np.savetxt(myFile, mean_PSNR_each, fmt='%.4f',header="\nmean PSNR for each focal stack")
            np.savetxt(myFile, test_PSNRs, fmt='%.4f',header="\nPSNR for each individual image")
        
            myFile.close()        
            print("mean_PSNR_all:", mean_PSNR_all)
            print("mean_PSNR_each:\n", mean_PSNR_each)        
            print("testing is done!")

            test_SSIMs = np.array(test_SSIMs)
            mean_SSIM_all = np.mean(test_SSIMs)
            mean_SSIM_each = np.mean(test_SSIMs, axis=1)
            myFile = open(SSIM_FN, 'wb')
            np.savetxt(myFile, mean_SSIM_all.reshape((1,1)), fmt='%.4f',header="\nmean SSIM for all test images")
            np.savetxt(myFile, mean_SSIM_each, fmt='%.4f',header="\nmean SSIM for each focal stack")
            np.savetxt(myFile, test_SSIMs, fmt='%.4f',header="\nSSIM for each individual image")

            myFile.close()        
            print("mean_SSIM_all:", mean_SSIM_all)
            print("mean_SSIM_each:\n", mean_SSIM_each)        
            
        print("test is done!")







