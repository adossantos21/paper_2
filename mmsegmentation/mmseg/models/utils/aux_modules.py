'''
Auxiliary modules for semantic segmentation models.
For PIDNet, we have the P Branch and the D Branch modules.
For Semantic Boundary Detection (SBD), we have the CASENet, DFF, and BGF modules.
'''

# Copyright (c) OpenMMLab. All rights reserved.
from typing import Tuple, Union

import torch
import torch.nn as nn
import torch.nn.functional as F
from mmcv.cnn import ConvModule
from torch import Tensor

from mmseg.registry import MODELS
from mmseg.utils import OptConfigType
from mmseg.models.utils import BasicBlock, Bottleneck
from .base import CustomBaseModule
from .fusion_modules import PagFM


class PModule(CustomBaseModule):
    '''
    Model layers for the P branch of PIDNet. 

    Args:
        in_channels (int): The number of input channels. Default: 3.
        channels (int): The number of channels in the stem layer. Default: 64.
        ppm_channels (int): The number of channels in the PPM layer.
            Default: 96.
        num_stem_blocks (int): The number of blocks in the stem layer.
            Default: 2.
        num_branch_blocks (int): The number of blocks in the branch layer.
            Default: 3.
        align_corners (bool): The align_corners argument of F.interpolate.
            Default: False.
        norm_cfg (dict): Config dict for normalization layer.
            Default: dict(type='BN').
        act_cfg (dict): Config dict for activation layer.
            Default: dict(type='ReLU', inplace=True).
        init_cfg (dict): Config dict for initialization. Default: None.
    '''
    # Optionally add argument `train` to constructor and pass `self.training` to it from the appropriate head module.
    # Another option is to register these modules if you need to.
    def __init__(self,
                 channels: int = 64,
                 num_stem_blocks: int = 2,
                 align_corners: bool = False,
                 init_cfg: OptConfigType = None,
                 **kwargs):
        super().__init__(init_cfg)
        self.align_corners = align_corners

        self.relu = nn.ReLU()

        # P Branch
        self.p_branch_layers = nn.ModuleList()
        for i in range(3):
            self.p_branch_layers.append(
                self._make_layer(
                    block=BasicBlock if i < 2 else Bottleneck,
                    in_channels=channels * 2,
                    channels=channels * 2,
                    num_blocks=num_stem_blocks if i < 2 else 1))
        self.compression_1 = ConvModule(
            channels * 4,
            channels * 2,
            kernel_size=1,
            bias=False,
            norm_cfg=self.norm_cfg,
            act_cfg=None)
        self.compression_2 = ConvModule(
            channels * 8,
            channels * 2,
            kernel_size=1,
            bias=False,
            norm_cfg=self.norm_cfg,
            act_cfg=None)
        self.pag_1 = PagFM(channels * 2, channels)
        self.pag_2 = PagFM(channels * 2, channels)

    def forward(self, x: Tensor) -> Union[Tensor, Tuple[Tensor]]:
        """Forward function.
        
        NOTE: self.training is inherent to MMSeg configurations throughout BaseModule 
        and BaseDecodeHead objects. Its boolean is inherited based on whether the
        train loop or the test/val loops are executing.
        
        Args:
            x (Tensor): Input tensor with shape (B, C, H, W).

        Returns:
            Tensor or tuple[Tensor]: If self.training is True, return
                tuple[Tensor], else return Tensor.
        """
        x_2, x_3, x_4, _ = x

        # stage 3
        x_p = self.p_branch_layers[0](x_2)

        comp_i = self.compression_1(x_3)
        x_p = self.pag_1(x_p, comp_i)
        if self.training:
            temp_p = x_p.clone() # (N, 128, H/8, W/8)

        # stage 4
        x_p = self.p_branch_layers[1](self.relu(x_p))

        comp_i = self.compression_2(x_4)
        x_p = self.pag_2(x_p, comp_i)

        # stage 5
        x_p = self.p_branch_layers[2](self.relu(x_p)) # (N, 256, H/8, W/8)
        
        return tuple([temp_p, x_p]) if self.training else x_p


class DModule(CustomBaseModule):
    '''
    Model layers for the D branch of PIDNet.
    '''
    def __init__(self,
                 channels: int = 64,
                 num_stem_blocks: int = 2,
                 align_corners: bool = False,
                 norm_cfg: OptConfigType = dict(type='BN'),
                 act_cfg: OptConfigType = dict(type='ReLU', inplace=True),
                 init_cfg: OptConfigType = None,
                 **kwargs):
        super().__init__(init_cfg)
        self.norm_cfg = norm_cfg
        self.act_cfg = act_cfg
        self.align_corners = align_corners

        self.relu = nn.ReLU()

        # D Branch
        if num_stem_blocks == 2:
            self.d_branch_layers = nn.ModuleList([
                self._make_single_layer(BasicBlock, channels * 2, channels),
                self._make_layer(Bottleneck, channels, channels, 1)
            ])
            channel_expand = 1
        else:
            self.d_branch_layers = nn.ModuleList([
                self._make_single_layer(BasicBlock, channels * 2,
                                        channels * 2),
                self._make_single_layer(BasicBlock, channels * 2, channels * 2)
            ])
            channel_expand = 2

        self.diff_1 = ConvModule(
            channels * 4,
            channels * channel_expand,
            kernel_size=3,
            padding=1,
            bias=False,
            norm_cfg=norm_cfg,
            act_cfg=None)
        self.diff_2 = ConvModule(
            channels * 8,
            channels * 2,
            kernel_size=3,
            padding=1,
            bias=False,
            norm_cfg=norm_cfg,
            act_cfg=None)

        self.d_branch_layers.append(
            self._make_layer(Bottleneck, channels * 2, channels * 2, 1))

    def forward(self, x: Tensor) -> Union[Tensor, Tuple[Tensor]]:
        """Forward function.

        Args:
            x (Tensor): Input tensor with shape (B, C, H, W).

        Returns:
            Tensor or tuple[Tensor]: If self.training is True, return
                tuple[Tensor], else return Tensor.
        """
        x_2, x_3, x_4, _ = x

        w_out = x.shape[-1] // 8
        h_out = x.shape[-2] // 8


        # stage 3
        x_d = self.d_branch_layers[0](x_2)

        diff_i = self.diff_1(x_3)
        x_d += F.interpolate(
            diff_i,
            size=[h_out, w_out],
            mode='bilinear',
            align_corners=self.align_corners)

        # stage 4
        x_d = self.d_branch_layers[1](self.relu(x_d))

        diff_i = self.diff_2(x_4)
        x_d += F.interpolate(
            diff_i,
            size=[h_out, w_out],
            mode='bilinear',
            align_corners=self.align_corners)
        if self.training:
            temp_d = x_d.clone()

        # stage 5
        x_d = self.d_branch_layers[2](self.relu(x_d))

        return temp_d, x_d if self.training else x_d

class CASENet(CustomBaseModule):
    '''
    Model layers for the CASENet SBD module.
    '''
    def __init__(self, nclass, norm_layer=nn.BatchNorm2d, **kwargs):
        super(CASENet, self).__init__(nclass, norm_layer=norm_layer, **kwargs)

        self.side1 = nn.Conv2d(64, 1, 1)
        self.side2 = nn.Sequential(nn.Conv2d(256, 1, 1, bias=True),
                                   nn.ConvTranspose2d(1, 1, 4, stride=2, padding=1, bias=False))
        self.side3 = nn.Sequential(nn.Conv2d(512, 1, 1, bias=True),
                                   nn.ConvTranspose2d(1, 1, 8, stride=4, padding=2, bias=False))
        self.side5 = nn.Sequential(nn.Conv2d(1024, nclass, 1, bias=True), # originally, 1024 was 2048; changed due to PIDNet architecture
                                   nn.ConvTranspose2d(nclass, nclass, 16, stride=8, padding=4, bias=False))
        self.fuse = nn.Conv2d(nclass*4, nclass, 1, groups=nclass, bias=True)

    def forward(self, x):
        c1, c2, c3, _, c5, _ = x

        side1 = self.side1(c1)
        side2 = self.side2(c2)
        side3 = self.side3(c3)
        side5 = self.side5(c5)

        slice5 = side5[:,0:1,:,:]
        fuse = torch.cat((slice5, side1, side2, side3), 1)
        for i in range(side5.size(1)-1):
            slice5 = side5[:,i+1:i+2,:,:]
            fuse = torch.cat((fuse, slice5, side1, side2, side3), 1)

        fuse = self.fuse(fuse)

        outputs = [side5, fuse]

        return tuple(outputs)
    
class DFF(CustomBaseModule):
    '''
    Model layers for the Dynamic Feature Fusion (DFF) SBD module.
    '''
    def __init__(self, nclass, norm_layer=nn.BatchNorm2d, **kwargs):
        super(DFF, self).__init__(nclass, norm_layer=norm_layer, **kwargs)
        self.nclass = nclass
        self.ada_learner = LocationAdaptiveLearner(nclass, nclass*4, nclass*4, norm_layer=norm_layer)
        self.side1 = nn.Sequential(nn.Conv2d(64, 1, 1),
                                   norm_layer(1))
        self.side2 = nn.Sequential(nn.Conv2d(256, 1, 1, bias=True),
                                   norm_layer(1),
                                   nn.ConvTranspose2d(1, 1, 4, stride=2, padding=1, bias=False))
        self.side3 = nn.Sequential(nn.Conv2d(512, 1, 1, bias=True),
                                   norm_layer(1),
                                   nn.ConvTranspose2d(1, 1, 8, stride=4, padding=2, bias=False))
        self.side5 = nn.Sequential(nn.Conv2d(1024, nclass, 1, bias=True), # originally, 1024 was 2048; changed due to PIDNet architecture
                                   norm_layer(nclass),
                                   nn.ConvTranspose2d(nclass, nclass, 16, stride=8, padding=4, bias=False))

        self.side5_w = nn.Sequential(nn.Conv2d(1024, nclass*4, 1, bias=True), # originally, 1024 was 2048; changed due to PIDNet architecture
                                   norm_layer(nclass*4),
                                   nn.ConvTranspose2d(nclass*4, nclass*4, 16, stride=8, padding=4, bias=False))

    def forward(self, x):
        c1, c2, c3, _, c5, _ = x
        side1 = self.side1(c1) # (N, 1, H, W)
        side2 = self.side2(c2) # (N, 1, H, W)
        side3 = self.side3(c3) # (N, 1, H, W)
        side5 = self.side5(c5) # (N, 19, H, W)
        side5_w = self.side5_w(c5) # (N, 19*4, H, W)
        
        ada_weights = self.ada_learner(side5_w) # (N, 19, 4, H, W)

        slice5 = side5[:,0:1,:,:] # (N, 1, H, W)
        fuse = torch.cat((slice5, side1, side2, side3), 1)
        for i in range(side5.size(1)-1):
            slice5 = side5[:,i+1:i+2,:,:] # (N, 1, H, W)
            fuse = torch.cat((fuse, slice5, side1, side2, side3), 1) # (N, 19*4, H, W)

        fuse = fuse.view(fuse.size(0), self.nclass, -1, fuse.size(2), fuse.size(3)) # (N, 19, 4, H, W)
        fuse = torch.mul(fuse, ada_weights) # (N, 19, 4, H, W)
        fuse = torch.sum(fuse, 2) # (N, 19, H, W)

        outputs = [side5, fuse]

        return tuple(outputs)


class BEM(CustomBaseModule):
    '''
    Model layers for DCBNetv1's SBD module, Boundary Extraction Module (BEM).
    '''
    def __init__(self, planes=64, norm_layer=nn.BatchNorm2d):
        self.norm_layer = norm_layer

        self.side1 = nn.Sequential(nn.Conv2d(in_channels=planes, out_channels=planes*2, kernel_size=3, stride=2, padding=1), # (N, 128, H/8, W/8)
                                    self.norm_layer(num_features=planes*2))
        self.side2 = nn.Sequential(nn.Conv2d(in_channels=planes*2, out_channels=planes*2, kernel_size=3, padding=1, bias=True), # (N, C=1, H/8, W/8)
                                    self.norm_layer(num_features=planes*2))
        self.side3 = nn.Sequential(nn.Conv2d(in_channels=planes*4, out_channels=planes*2, kernel_size=3, padding=1, bias=True), # (N, C=1, H/8, W/8)
                                    self.norm_layer(num_features=planes*2))
        self.side5 = nn.Sequential(nn.Conv2d(in_channels=planes*16, out_channels=planes*2, kernel_size=3, padding=1, bias=True), # (N, C=19, H/8, W/8)
                                    self.norm_layer(num_features=planes*2))
        self.side5_w = nn.Sequential(nn.Conv2d(in_channels=planes*16, out_channels=planes*8, kernel_size=3, padding=1, bias=True), # (N, C=19*4, H/8, W/8)
                                    self.norm_layer(num_features=planes*8))

        self.layer1 = self._make_single_layer(BasicBlock, planes * 2, planes * 2) 
        self.layer2 = self._make_single_layer(BasicBlock, planes * 2, planes * 2)

        # No ReLU because we want side5 and fuse to have similar sequences and consequent responses
        self.sep_conv = nn.Sequential(
            nn.Conv2d(in_channels=planes*8, out_channels=planes*8, kernel_size=3, padding=1, groups=planes*8, bias=True),
            nn.Conv2d(in_channels=planes*8, out_channels=planes*2, kernel_size=1, bias=True),
            nn.BatchNorm2d(num_features=planes*2),
            nn.ReLU(inplace=True)
        )

        self.adaptive_learner = LocationAdaptiveLearner(planes*2, planes*8, planes*8, norm_layer=self.norm_layer)

    def forward(self, x):
        '''
        c1 has shape (N, 64, H/4, W/4)
        c2 has shape (N, 128, H/8, W/8)
        c3 has shape (N, 256, H/16, W/16)
        c4 has shape (N, 512, H/32, W/32)
        c5 has shape (N, 1024, H/64, W/64)
        '''
        c1, c2, c3, _, c5, _ = x
        '''Stage 1'''
        Aside1 = self.side1(c1) # (N, 128, H/8, W/8), may need to clone input

        '''Stage 2'''
        Aside2 = self.side2(c2) # (N, 128, H/8, W/8)
        Aside2 = self.layer1(Aside1 + Aside2) # (N, 128, H/8, W/8)
        height, width = Aside2.shape[2:]

        '''Stage 3'''
        Aside3 = F.interpolate(self.side3(c3), # (N, 128, H/8, W/8)
                               size=[height, width],
                               mode='bilinear', align_corners=False)
        Aside3 = self.layer2(Aside3 + Aside2) # (N, 128, H/8, W/8)
        
        '''Stage 5'''
        Aside5 = Aside3 + F.interpolate(self.side5(c5), # (N, 128, H/8, W/8)
                                        size=[height, width],
                                        mode='bilinear', align_corners=False)

        Aside5_w = F.interpolate(self.side5_w(c5), # (N, 512, H/8, W/8)
                        size=[height, width],
                        mode='bilinear', align_corners=False)
        
        '''Fuse Sides 1-3 and 5'''
        adaptive_weights = F.softmax(self.adaptive_learner(Aside5_w), dim=2) # (N, 128, 4, H/8, W/8), softmax forces learned weights of each Aside to be mutually exclusive along the fusion dimension.
        concat = torch.cat((Aside1, Aside2, Aside3, Aside5), dim=1) # (N, 512, H/8, W/8)
        edge_5d = concat.view(concat.size(0), -1, 4, concat.size(2), concat.size(3)) # (N, 128, 4, H/8, W/8)
        fuse = torch.mul(edge_5d, adaptive_weights) # (N, 128, 4, H/8, W/8)
        fuse = fuse.view(fuse.size(0), -1, fuse.size(3), fuse.size(4)) # (N, 512, H/8, W/8)
        fuse = self.sep_conv(fuse) # (N, 128, H/8, W/8)

        outputs = [Aside5, fuse]
        
        return tuple(outputs)

class LocationAdaptiveLearner(nn.Module):
    """docstring for LocationAdaptiveLearner"""
    def __init__(self, nclass, in_channels, out_channels, norm_layer=nn.BatchNorm2d):
        super(LocationAdaptiveLearner, self).__init__()
        self.nclass = nclass

        self.conv1 = nn.Sequential(nn.Conv2d(in_channels, out_channels, 1, bias=True),
                                   norm_layer(out_channels),
                                   nn.ReLU(inplace=True))
        self.conv2 = nn.Sequential(nn.Conv2d(out_channels, out_channels, 1, bias=True),
                                   norm_layer(out_channels),
                                   nn.ReLU(inplace=True))
        self.conv3 = nn.Sequential(nn.Conv2d(out_channels, out_channels, 1, bias=True),
                                   norm_layer(out_channels))

    def forward(self, x):
        # x:side5_w (N, 19*4, H, W)
        x = self.conv1(x) # (N, 19*4, H, W)
        x = self.conv2(x) # (N, 19*4, H, W)
        x = self.conv3(x) # (N, 19*4, H, W)
        x = x.view(x.size(0), self.nclass, -1, x.size(2), x.size(3)) # (N, 19, 4, H, W)
        return x