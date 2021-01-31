import helpers
from spatialmath.base import r2q, tr2eul, tr2rpy
import numpy as np
import os
import cv2
import torch
from torch.utils.data import Dataset


# Class for providing an iterator for the KITTI visual odometry dataset
class KITTIDataset(Dataset):

    # Constructor
    def __init__(self, kitti_base_dir, sequences=None, startFrames=None, endFrames=None,
                 parameterization='default', width=1280, height=384):

        # Path to base directory of the KITTI odometry dataset
        # The base directory contains two directories: 'poses' and 'sequences'
        # The 'poses' directory contains text files that contain ground-truth pose
        # for the train sequences (00-10). The 11 train sequences and 11 test sequences
        # are present in the 'sequences' folder
        self.baseDir = kitti_base_dir

        # Path to directory containing images
        self.imgDir = os.path.join(self.baseDir, 'sequences')
        # Path to directory containing pose ground-truth
        self.poseDir = os.path.join(self.baseDir, 'poses')

        # Max frames in each KITTI sequence
        self.KITTIMaxFrames = [4540, 1100, 4660, 800, 270, 2760, 1100, 1100, 4070, 1590, 1200]

        # # Mean of R, G, B color channel values
        # self.channelwiseMean = [88.61, 93.70, 92.11]
        # # Standard deviation of R, G, B color channel values
        # self.channelwiseStdDev = [79.35914872, 80.69872125, 82.34685558]

        self.channelwiseMean = [0.0, 0.0, 0.0]
        self.channelwiseStdDev = [1.0, 1.0, 1.0]

        # Dimensions to be fed in the input
        self.width = width
        self.height = height
        self.channels = 3

        # List of sequences that are part of the dataset
        # If nothing is specified, use sequence 1 as default
        self.sequences = sequences if sequences is not None else [1]

        # List of start frames and end frames for each sequence
        self.startFrames = startFrames if startFrames is not None else [0]
        self.endFrames = endFrames if endFrames is not None else [1100]

        # Parameterization to be used to represent the transformation
        self.parameterization = parameterization
        # Variable to hold length of the dataset
        self.length = 0
        # Variables used as caches to implement quick __getitem__ retrieves
        self.cumulativeLengths = [0] * len(self.sequences)

        # Check if the parameters passed are consistent. Throw an error otherwise
        # KITTI has ground-truth pose information only for sequences 00 to 10
        if min(self.sequences) < 0 or max(self.sequences) > 10:
            raise ValueError('Sequences must be within the range [00-10]')
        if len(self.sequences) != len(self.startFrames):
            raise ValueError('There are not enough startFrames specified as there are sequences.')
        if len(self.sequences) != len(self.endFrames):
            raise ValueError('There are not enough endFrames specified as there are sequences.')
        # Check that, for each sequence, the corresponding start and end frames are within limits
        for i in range(len(self.sequences)):
            seq = self.sequences[i]
            if self.startFrames[i] < 0 or self.startFrames[i] > self.KITTIMaxFrames[seq]:
                raise ValueError('Invalid startFrame for sequence', str(seq).zfill(2))
            if self.endFrames[i] < 0 or self.endFrames[i] <= self.startFrames[i] or \
                    self.endFrames[i] > self.KITTIMaxFrames[seq]:
                raise ValueError('Invalid endFrame for sequence', str(seq).zfill(2))
            self.length += (endFrames[i] - startFrames[i])
            self.cumulativeLengths[i] = self.length
        if self.length < 0:
            raise ValueError('Length of the dataset cannot be negative.')

    # Get dataset size
    def __len__(self):
        return self.length

    def __getitem__(self, idx):

        # First determine which sequence the index belongs to, using self.cumulativeLengths
        seqKey = helpers.first_ge(self.cumulativeLengths, idx)
        seqIdx = self.sequences[seqKey]

        # Now select the offset from the first frame of the sequence that the current idx
        # belongs to
        if seqKey == 0:
            offset = idx
        else:
            offset = idx - self.cumulativeLengths[seqKey - 1]

        # Map the offset to frame ids
        frame1 = self.startFrames[seqKey] + offset
        frame2 = frame1 + 1

        # Flag to indicate end of sequence
        endOfSequence = False
        if frame2 == self.endFrames[seqKey]:
            endOfSequence = True

        # return (seqIdx, frame1, frame2)

        # Directory containing images for the current sequence
        curImgDir = os.path.join(self.imgDir, str(seqIdx).zfill(2), 'image_2')
        # Read in the corresponding images
        img1 = cv2.cvtColor(cv2.imread(os.path.join(curImgDir, str(frame1).zfill(6) + '.png')), cv2.COLOR_BGR2RGB)
        img2 = cv2.cvtColor(cv2.imread(os.path.join(curImgDir, str(frame2).zfill(6) + '.png')), cv2.COLOR_BGR2RGB)
        # Preprocess : returned after mean subtraction, resize and permute to N x C x W x H dims
        img1 = self.preprocess_img(img1)
        img2 = self.preprocess_img(img2)

        # Concatenate the images along the channel dimension (and CUDAfy them)
        pair = torch.empty([1, 2 * self.channels, self.height, self.width])
        pair[0] = torch.cat((img1, img2), 0)
        inputTensor = (pair.float()).cuda()
        inputTensor = inputTensor * torch.from_numpy(np.asarray([1. / 255.], dtype=np.float32)).cuda()

        # Load pose ground-truth
        poses = np.loadtxt(os.path.join(self.poseDir, str(seqIdx).zfill(2) + '.txt'), dtype=np.float32)
        pose1 = np.vstack([np.reshape(poses[frame1].astype(np.float32), (3, 4)), [[0., 0., 0., 1.]]])
        pose2 = np.vstack([np.reshape(poses[frame2].astype(np.float32), (3, 4)), [[0., 0., 0., 1.]]])
        # Compute relative pose from frame1 to frame2
        pose2wrt1 = np.dot(np.linalg.inv(pose1), pose2)
        R = pose2wrt1[0:3, 0:3]
        t = (torch.from_numpy(pose2wrt1[0:3, 3]).view(-1, 3)).float().cuda()
        T = np.concatenate(
            (np.concatenate([R, np.reshape(pose2wrt1[0:3, 3], (3, -1))], axis=1), [[0.0, 0.0, 0.0, 1.0]]), axis=0)

        # Default parameterization: representation rotations as axis-angle vectors
        if self.parameterization == 'default':
            axisAngle = (torch.from_numpy(np.asarray(tr2rpy(R))).view(-1, 3)).float().cuda()
            return inputTensor, axisAngle, t, seqIdx, frame1, frame2, endOfSequence
        # Quaternion parameterization: representation rotations as quaternions
        elif self.parameterization == 'quaternion':
            quat = np.asarray(r2q(R)).reshape((1, 4))
            quaternion = (torch.from_numpy(quat).view(-1, 4)).float().cuda()
            return inputTensor, quaternion, t, seqIdx, frame1, frame2, endOfSequence
        # Euler parameterization: representation rotations as Euler angles
        elif self.parameterization == 'euler':
            rx, ry, rz = tr2eul(R, seq='xyz')
            euler = (10. * torch.FloatTensor([rx, ry, rz]).view(-1, 3)).cuda()
            return inputTensor, euler, t, seqIdx, frame1, frame2, endOfSequence
        elif self.parameterization == 'se3':

            R = pose2[0:3, 0:3]
            t = (torch.from_numpy(pose2[0:3, 3]).view(-1, 3)).float().cuda()
            quat = np.asarray(r2q(R)).reshape((1, 4))
            quaternion = (torch.from_numpy(quat).view(-1, 4)).float().cuda()
            return inputTensor, quaternion, t, seqIdx, frame1, frame2, endOfSequence

    # return (seqIdx, frame1, frame2)

    # Center and scale the image, resize and perform other preprocessing tasks
    def preprocess_img(self, img):

        # Subtract the mean R,G,B pixels
        img[:, :, 0] = (img[:, :, 0] - self.channelwiseMean[0]) / (self.channelwiseStdDev[0])
        img[:, :, 1] = (img[:, :, 1] - self.channelwiseMean[1]) / (self.channelwiseStdDev[1])
        img[:, :, 2] = (img[:, :, 2] - self.channelwiseMean[2]) / (self.channelwiseStdDev[2])

        # Resize to the dimensions required
        img = np.resize(img, (self.height, self.width, self.channels))

        # Torch expects NCWH
        img = torch.from_numpy(img)
        img = img.permute(2, 0, 1)

        return img
