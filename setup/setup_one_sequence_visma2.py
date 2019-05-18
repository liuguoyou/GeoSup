""" Data preparation script for training depth completion on VOID dataset.
Requirements: vlslam_pb2 generated by vlslam.proto and ROS.
Make sure you have the raw rosbag recorded and dataset file generated by vlslam.
Author: Xiaohan Fei
"""
import argparse, os, sys
import numpy as np
from scipy.interpolate import LinearNDInterpolator
from scipy.spatial.qhull import QhullError
import matplotlib.pyplot as plt
from absl import logging
from collections import deque as CircularBuffer
import tempfile, shutil
import pickle
# ros
import cv2
import rosbag
from sensor_msgs.msg import Image
from cv_bridge import CvBridge
# corvis
import vlslam_pb2

# some constants
MIN_Z = 0.2
MAX_Z = 5.0
valid_status = [vlslam_pb2.Feature.READY,
        vlslam_pb2.Feature.KEEP, vlslam_pb2.Feature.INSTATE, vlslam_pb2.Feature.GOODDROP]

def compose_pose(g1, g2):
  """ Compose two 3x4 pose matrices.
  """
  g = np.concatenate((g1[:3, :3].dot(g2[:3, :3]), g1[:3, :3].dot(g2[:3, [3]]) + g1[:3, [3]]), axis=1)
  if g1.shape[0] == 4:
    g = np.concatenate((g, [[0, 0, 0, 1]]), axis=0)
  return g

def inverse_pose(g1):
  """ Compute the inverse pose.
  g1 = [R|t]
  then g^{-1} = [R.T| -R.T * t]
  """
  Rt = g1[:3, :3].T
  g = np.concatenate((Rt, -Rt.dot(g1[:3, [3]])), axis=1)
  if g1.shape[0] == 4:
    g = np.concatenate((g, [[0, 0, 0, 1]]), axis=0)
  return g

def list2pose(v):
    """ Convert a list of 12 floating-point numbers to a 3x4 pose matrix.
    Args:
        v: list-like list of numbers
    Returns: 3x4 matrix [R|t]
    """
    return np.asarray(v).reshape((3, 4))

def list2R(wg):
    """ Convert 2-dim vector Wg to Rg, which is the rotation to align gravity to the canonical form [0, 0, 1]
    Args:
        wg: list-like list of numbers
    Returns: 3x3 matrix Rg
    """
    Rg, _ = cv2.Rodrigues(np.asarray(list(wg) + [0]))
    return Rg


def construct_triplet(circbuf, temporal_interval=5, spatial_interval=0.01):
    """ Construct triplets as needed by training code.
    Args:
        cirbuf: list-like circular buffer containing tuples of the form
            (rgb image, gwc, Rg, ground truth depth, output paths)
        temporal_interval: how many frames apart
        spatial_interval: to ensure enough parallex
    Returns: (3 rgb images concatenated along dimension 1,
        Nx3x4 concatenated gwc along dimension 1,
        Nx3x3 concatenated Rg along dimension 1,
        ground truth depth of the reference frame,
        output paths of the reference image)
    """
    ####################
    # concatenate pose
    ####################
    gwc_concat = np.stack([circbuf[temporal_interval * i][1] for i in range(3)], axis=0)
    Rg_concat = np.stack([circbuf[temporal_interval * i][2] for i in range(3)], axis=0)

    # compose
    g10 = compose_pose(inverse_pose(gwc_concat[1]), gwc_concat[0])
    g12 = compose_pose(inverse_pose(gwc_concat[1]), gwc_concat[2])
    if np.linalg.norm(g10[:3, 3]) < spatial_interval or np.linalg.norm(g12[:3, 3]) < spatial_interval:
        raise ValueError('Not enough parallel!!!')

    ####################
    # concatenate image
    ####################
    rgb_concat = np.concatenate([circbuf[i * temporal_interval][0] for i in range(3)], axis=1)
    # return
    return (rgb_concat,
            gwc_concat,
            Rg_concat,
            circbuf[temporal_interval][3],
            circbuf[temporal_interval][-1])

def process_one_sequence(opt):
    # bundle the 3 output folders in one to reduce hard driver addressing time in data loader?
    if len(opt.output_dir) == 0:
        output_dir = tempfile.mkdtemp(dir='.')
    else:
        output_dir = opt.output_dir

    rgb_output = os.path.join(output_dir, 'rgb')
    pose_output = os.path.join(output_dir, 'pose')
    # ground truth depth output
    depth_output = os.path.join(output_dir, 'depth')
    # create output directories
    for folder in [rgb_output, pose_output, depth_output]:
        if not os.path.exists(folder):
            logging.warn('folder {} does not exist; creating one ...'.format(folder))
            os.makedirs(folder)
    # load bags
    color_topic = '/camera/color/image_raw'
    depth_topic = '/camera/aligned_depth_to_color/image_raw'
    imu_topic = '/camera/imu'

    bag = rosbag.Bag(os.path.join(opt.work_dir, 'raw.bag'), 'r')
    bridge = CvBridge()
    # bag.read_messages returns a generator, which can be iterated through via .next() function
    color_messages = bag.read_messages(topics=[color_topic])
    depth_messages = bag.read_messages(topics=[depth_topic])

    # load dataset
    dataset = vlslam_pb2.Dataset()
    with open(os.path.join(opt.work_dir, 'dataset'), 'rb') as fid:
        dataset.ParseFromString(fid.read())
    cam = dataset.camera
    rows, cols = cam.rows, cam.cols
    K = np.array([[cam.radtan.fx, 0.0, cam.radtan.cx],
        [0, cam.radtan.fy, cam.radtan.cy],
        [0, 0, 1]])

    logging.info('saving camera intrinsics')
    np.save(os.path.join(output_dir, 'K'), K)

    dt = 0.025  # within this threshold, two items are considered being captured at the same time instant

    circbuf_maxlen = opt.temporal_interval * 2 + 1
    circbuf = CircularBuffer(maxlen=circbuf_maxlen)
    count = 0

    if opt.debug: plt.ion()
    for i, packet in enumerate(dataset.packets):
        now = packet.ts

        rgb_msg = color_messages.next()
        depth_msg = depth_messages.next()

        while rgb_msg.timestamp.to_sec() < now - dt:
            rgb_msg = color_messages.next()
            depth_msg = depth_messages.next()

        # while depth_msg.timestamp.to_sec() < now - dt:
        #     depth_msg = depth_messages.next()

        if rgb_msg.timestamp.to_sec() > now + dt:
            logging.warn('bad sample, skipping ...')
            continue

        count += 1

        # print('#{:04d}, [pose, rgb, depth_msg]=[{:0.4f}, {:0.4f},{:0.4f}]'.format(
        #     count, now, rgb_msg.timestamp.to_sec(), depth_msg.timestamp.to_sec()))

        rgb = bridge.imgmsg_to_cv2(rgb_msg.message, desired_encoding='passthrough')
        depth = bridge.imgmsg_to_cv2(depth_msg.message, desired_encoding='passthrough')
        depth = depth / 1000.0    # convert from millimeters to meters

        basename = '{:.4f}'.format(now)
        output_paths = {'rgb': os.path.join(rgb_output, basename + '.jpg'),
                'pose': os.path.join(pose_output, basename + '.pkl'),
                'depth': os.path.join(depth_output, basename + '.npy')}

        gwc = list2pose(packet.gwc)
        Rg = list2R(packet.wg)

        circbuf.append((rgb, gwc, Rg, depth, output_paths))
        if len(circbuf) == circbuf_maxlen:
            # construct triplet and dump
            try:
                rgb_concat, gwc_concat, Rg_concat, dense_ref, ref_paths = construct_triplet(
                        circbuf, opt.temporal_interval, opt.spatial_interval)
                # saving
                logging.info('saving ...')
                basename = '{:.4f}'.format(now)

                cv2.imwrite(ref_paths['rgb'], rgb_concat)
                # plt.imsave(ref_paths['rgb'], rgb_concat)
                with open(ref_paths['pose'], 'wb') as fid:
                    pickle.dump({'gwc': gwc_concat, 'Rg': Rg_concat}, fid, protocol=0)

                np.save(ref_paths['depth'], dense_ref.astype(np.float32))

                if opt.debug:
                    im0, im1, im2 = np.split(rgb_concat, 3, axis=1)
                    plt.clf()
                    plt.subplot(221)
                    plt.imshow(im1)
                    plt.title('t')

                    plt.subplot(222)
                    plt.imshow(dense_ref)
                    plt.title('dense depth of ref (mid)')

                    plt.subplot(223)
                    plt.imshow(im0)
                    plt.title('t-1')

                    plt.subplot(224)
                    plt.imshow(im2)
                    plt.title('t+1')

                    plt.pause(0.01)
            except ValueError:
                logging.warn('Not enough parallel; skip')