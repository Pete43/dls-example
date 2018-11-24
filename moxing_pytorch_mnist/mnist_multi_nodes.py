# coding:utf-8

# Copyright 2018 Deep Learning Service of Huawei Cloud. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import sys
import argparse
import multiprocessing as mp
import moxing as mox
from math import ceil

import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
import torch.distributed as dist
from torch.utils.data import distributed
from torchvision import datasets, transforms

parser = argparse.ArgumentParser()
parser.add_argument('--data_url', type=str, default=None, help='s3 path of dataset')
parser.add_argument('--train_url', type=str, default=None, help='s3 path of outputs')
parser.add_argument('--batch_size', type=int, default=64, help='batch_size')

parser.add_argument('--init_method', type=str, default=None, help='master address')
parser.add_argument('--rank', type=int, default=0, help='Index of current task')
parser.add_argument('--world_size', type=int, default=1, help='Total number of tasks')

args, unparsed = parser.parse_known_args()


class Net(nn.Module):
  """ Network architecture. """

  def __init__(self):
    super(Net, self).__init__()
    self.conv1 = nn.Conv2d(1, 10, kernel_size=5)
    self.conv2 = nn.Conv2d(10, 20, kernel_size=5)
    self.conv2_drop = nn.Dropout2d()
    self.fc1 = nn.Linear(320, 50)
    self.fc2 = nn.Linear(50, 10)

  def forward(self, x):
    x = F.relu(F.max_pool2d(self.conv1(x), 2))
    x = F.relu(F.max_pool2d(self.conv2_drop(self.conv2(x)), 2))
    x = x.view(-1, 320)
    x = F.relu(self.fc1(x))
    x = F.dropout(x, training=self.training)
    x = self.fc2(x)
    return F.log_softmax(x, dim=1)


def run():
  # Enable OBS access.
  mox.file.shift('os', 'mox')
  is_cuda = torch.cuda.is_available()

  dataset = datasets.MNIST(
    args.data_url,
    train=True,
    download=False,
    transform=transforms.Compose([
      transforms.ToTensor(),
      transforms.Normalize((0.1307,), (0.3081,))
    ]))
  train_sampler = distributed.DistributedSampler(dataset,
                                                 num_replicas=args.world_size,
                                                 rank=args.rank)
  train_set = torch.utils.data.DataLoader(dataset, batch_size=args.batch_size,
                                          shuffle=False, sampler=train_sampler,
                                          num_workers=14, pin_memory=True)

  num_batches = ceil(len(train_sampler) / float(args.batch_size))

  model = Net()
  if is_cuda:
    model = model.cuda()

  model = torch.nn.parallel.DistributedDataParallel(model)

  optimizer = optim.SGD(model.parameters(), lr=0.01, momentum=0.5)

  for epoch in range(10):
    epoch_loss = 0.0
    train_sampler.set_epoch(epoch)
    for data, target in train_set:
      optimizer.zero_grad()
      # move data to GPU:0 and then broadcast to all GPUs if available.
      data, target = (data.cuda(), target.cuda()) if is_cuda else (data, target)
      output = model(data)
      loss = F.nll_loss(output, target)
      # Sum up loss for one epoch and print the average value.
      epoch_loss += loss.data
      loss.backward()
      optimizer.step()
    print('epoch ', epoch, ' : ', epoch_loss / num_batches)

  if args.train_url and dist.get_rank() == 0:
    torch.save(model.state_dict(), args.train_url + 'model.pt')


def main():
  dist.init_process_group(backend='nccl',
                          init_method=args.init_method,
                          rank=args.rank,
                          world_size=args.world_size)
  run()


if __name__ == "__main__":
  if not (sys.version_info[0] >= 3 and sys.version_info[1] >= 4):
    raise ValueError('PyTorch distributed running with `DistributedDataParallel` '
                     'only support python >= 3.4')
  # change the multiprocessing start method to spawn before main is called, see:
  # https://pytorch.org/docs/stable/nn.html?highlight=distributeddataparallel#torch.nn.parallel.DistributedDataParallel
  mp.set_start_method('spawn')
  main()
