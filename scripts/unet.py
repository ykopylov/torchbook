
# https://github.com/mateuszbuda/brain-segmentation-pytorch/

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import Dataset

import numpy as np
import os
import random
import torchvision
from torchvision import datasets, models, transforms

from skimage.io import imread

exec(open('scripts/brainseg_utils.py').read())
exec(open('scripts/brainseg_transforms.py').read())


# ------------------------------------------------------------------------------

batch_size = 4

num_epochs = 1

learning_rate = 0.0001

#data_dir = "../data/lgg_mri_segmentation/kaggle_3m"
data_dir = "data/lgg-mri-segmentation/tmp"
image_size = 256

aug_scale = 0.05
aug_angle = 15

# ------------------- load data ------------------------------------------------

class BrainSegmentationDataset(Dataset):
    """Brain MRI dataset for FLAIR abnormality segmentation"""
    in_channels = 3
    out_channels = 1
    def __init__(
        self,
        images_dir,
        transform = None,
        image_size = 256,
        random_sampling = True,
    ):
        volumes = {}
        masks = {}
        print("reading images...")
        for (dirpath, dirnames, filenames) in os.walk(images_dir):
            image_slices = []
            mask_slices = []
            for filename in sorted(
                filter(lambda f: ".tif" in f, filenames),
                key=lambda x: int(x.split(".")[-2].split("_")[4]),
            ):
                filepath = os.path.join(dirpath, filename)
                if "mask" in filename:
                    mask_slices.append(imread(filepath, as_gray=True))
                else:
                    image_slices.append(imread(filepath))
            if len(image_slices) > 0:
                patient_id = dirpath.split("/")[-1]
                volumes[patient_id] = np.array(image_slices[1:-1])
                masks[patient_id] = np.array(mask_slices[1:-1])
        self.patients = sorted(volumes)
        print("preprocessing volumes...")
        # create list of tuples (volume, mask)
        self.volumes = [(volumes[k], masks[k]) for k in self.patients]
        print("cropping volumes...")
        # crop to smallest enclosing volume
        self.volumes = [crop_sample(v) for v in self.volumes]
        print("padding volumes...")
        # pad to square
        self.volumes = [pad_sample(v) for v in self.volumes]
        print("resizing volumes...")
        # resize
        self.volumes = [resize_sample(v, size=image_size) for v in self.volumes]
        print("normalizing volumes...")
        # normalize channel-wise
        self.volumes = [(normalize_volume(v), m) for v, m in self.volumes]
        # probabilities for sampling slices based on masks
        self.slice_weights = [m.sum(axis=-1).sum(axis=-1) for v, m in self.volumes]
        self.slice_weights = [
            (s + (s.sum() * 0.1 / len(s))) / (s.sum() * 1.1) for s in self.slice_weights
        ]
        # add channel dimension to masks
        self.volumes = [(v, m[..., np.newaxis]) for (v, m) in self.volumes]
        print("done creating dataset")
        # create global index for patient and slice (idx -> (p_idx, s_idx))
        num_slices = [v.shape[0] for v, m in self.volumes]
        self.patient_slice_index = list(
            zip(
                sum([[i] * num_slices[i] for i in range(len(num_slices))], []),
                sum([list(range(x)) for x in num_slices], []),
            )
        )
        self.random_sampling = random_sampling
        self.transform = transform
    def __len__(self):
        return len(self.patient_slice_index)
    def __getitem__(self, idx):
        patient = self.patient_slice_index[idx][0]
        slice_n = self.patient_slice_index[idx][1]
        if self.random_sampling:
            patient = np.random.randint(len(self.volumes))
            slice_n = np.random.choice(
                range(self.volumes[patient][0].shape[0]), p=self.slice_weights[patient]
            )
        v, m = self.volumes[patient]
        image = v[slice_n]
        mask = m[slice_n]
        if self.transform is not None:
            image, mask = self.transform((image, mask))
        # fix dimensions (C, H, W)
        image = image.transpose(2, 0, 1)
        mask = mask.transpose(2, 0, 1)
        image_tensor = torch.from_numpy(image.astype(np.float32))
        mask_tensor = torch.from_numpy(mask.astype(np.float32))
        # return tensors
        return image_tensor, mask_tensor

train_ds = BrainSegmentationDataset(
        images_dir = data_dir,
        image_size = image_size,
        transform = transforms(scale = aug_scale, angle = aug_angle, flip_prob=0.5),
)

valid_ds = BrainSegmentationDataset(
        images_dir = data_dir,
        image_size = image_size,
        random_sampling=False
)

train_loader = torch.utils.data.DataLoader(
        train_ds,
        batch_size = batch_size,
        shuffle = True,
        drop_last = True,
        num_workers = 4
)

valid_loader = torch.utils.data.DataLoader(
        valid_ds,
        batch_size = batch_size,
        drop_last = False,
)

dataloaders = {"train": train_loader, "valid": valid_loader}
image, mask = next(iter(train_loader))
image.size()
mask.size()

len(train_loader)
len(valid_loader)

class UNet(nn.Module):
    def __init__(
        self,
        channels_in = 3,
        channels_out = 1,
        depth = 5,
        n_filters = 6, 
    ):
        super(UNet, self).__init__()
        self.depth = depth
        prev_channels = channels_in
        self.down_path = nn.ModuleList()
        for i in range(depth):
            self.down_path.append(
                ConvBlock(prev_channels, 2 ** (n_filters + i))
            )
            prev_channels = 2 ** (n_filters + i)
        self.up_path = nn.ModuleList()
        for i in reversed(range(depth - 1)):
            self.up_path.append(
                UpBlock(prev_channels, 2 ** (n_filters + i))
            )
            prev_channels = 2 ** (n_filters + i)
        self.last = nn.Conv2d(prev_channels, channels_out, kernel_size = 1)
    def forward(self, x):
        blocks = []
        for i, down in enumerate(self.down_path):
            x = down(x)
            print("after down: x is {}".format(x.size()))
            if i != len(self.down_path) - 1:
                blocks.append(x)
                x = F.max_pool2d(x, 2)
                print("after maxpool: x is {}".format(x.size()))
        for i, up in enumerate(self.up_path):
            x = up(x, blocks[-i - 1])
            print("after up: x is {}".format(x.size()))
        return self.last(x)

class ConvBlock(nn.Module):
    def __init__(self, in_size, out_size):
        super(ConvBlock, self).__init__()
        block = []
        block.append(nn.Conv2d(in_size, out_size, kernel_size = 3, padding = 1))
        block.append(nn.ReLU())
        block.append(nn.Conv2d(out_size, out_size, kernel_size = 3, padding = 1))
        block.append(nn.ReLU())
        self.block = nn.Sequential(*block)
    def forward(self, x):
        out = self.block(x)
        return out

class UpBlock(nn.Module):
    def __init__(self, in_size, out_size):
        super(UpBlock, self).__init__()
        self.up = nn.ConvTranspose2d(in_size, out_size, kernel_size = 2, stride = 2)
        self.conv_block = ConvBlock(in_size, out_size)
    def forward(self, x, bridge):
        up = self.up(x)
        print("in upblock forward: up is {}".format(up.size()))
        print("in upblock forward: bridge is {}".format(bridge.size()))
        out = torch.cat([up, bridge], 1)
        print("in upblock forward: concatenated is {}".format(out.size()))
        out = self.conv_block(out)
        print("in upblock forward: out is {}".format(out.size()))
        return out


device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
model = UNet(depth = 5).to(device)


class DiceLoss(nn.Module):
    def __init__(self):
        super(DiceLoss, self).__init__()
        self.smooth = 1.0
    def forward(self, y_pred, y_true):
        assert y_pred.size() == y_true.size()
        y_pred = y_pred[:, 0].contiguous().view(-1)
        y_true = y_true[:, 0].contiguous().view(-1)
        intersection = (y_pred * y_true).sum()
        dsc = (2. * intersection + self.smooth) / (
            y_pred.sum() + y_true.sum() + self.smooth
        )
        return 1. - dsc


dsc_loss = DiceLoss()
optimizer = torch.optim.Adam(model.parameters(), lr = learning_rate)

step = 0

for epoch in range(num_epochs):
  print('Epoch/{}'.format(epoch, num_epochs - 1))
  print('-' * 10)
  #for phase in ["train", "valid"]:
  for phase in ["train"]:
    if phase == "train": model.train()
    else: model.eval()
    for i, data in enumerate(dataloaders[phase]):
      if phase == "train": step += 1
      x, y_true = data
      x, y_true = x.to(device), y_true.to(device)
      optimizer.zero_grad()
      with torch.set_grad_enabled(phase == "train"):
        y_pred = model(x)
        loss = dsc_loss(y_pred, y_true)
        if phase == "train":
          loss.backward()
          optimizer.step()
        #if (step + 1) % 10 == 0: print("{:s} loss: {:4f}".format(phase, loss))
        #if True: print("{:s} loss: {:4f}".format(phase, loss))


    