import collections
import math

import torch
import torchvision
from torch import nn
from torch.nn import functional as F 


def weights_init(modules, type='xavier'):
    m = modules

    def init(m):
        if isinstance(m, nn.Conv2d):
            if type == 'xavier':
                torch.nn.init.xavier_normal_(m.weight)
            elif type == 'kaiming':  # msra
                torch.nn.init.kaiming_normal_(m.weight)
            else:
                n = m.kernel_size[0] * m.kernel_size[1] * m.out_channels
                m.weight.data.normal_(0, math.sqrt(2. / n))

            if m.bias is not None:
                m.bias.data.zero_()
        elif isinstance(m, nn.ConvTranspose2d):
            if type == 'xavier':
                torch.nn.init.xavier_normal_(m.weight)
            elif type == 'kaiming':  # msra
                torch.nn.init.kaiming_normal_(m.weight)
            else:
                n = m.kernel_size[0] * m.kernel_size[1] * m.out_channels
                m.weight.data.normal_(0, math.sqrt(2. / n))

            if m.bias is not None:
                m.bias.data.zero_()
        elif isinstance(m, nn.BatchNorm2d):
            m.weight.data.fill_(1.0)
            m.bias.data.zero_()
        elif isinstance(m, nn.Linear):
            if type == 'xavier':
                torch.nn.init.xavier_normal_(m.weight)
            elif type == 'kaiming':
                torch.nn.init.kaiming_normal_(m.weight)
            else:
                m.weight.data.fill_(1.0)

            if m.bias is not None:
                m.bias.data.zero_()

    if (isinstance(m, nn.Conv2d) or isinstance(m, nn.ConvTranspose2d)
        or isinstance(m, nn.BatchNorm2d) or isinstance(m, nn.Linear)):
        init(m)
    elif isinstance(m, nn.Module):
        for m in modules:
            init(m)


class FullImageEncoder(nn.Module):
    def __init__(self):
        super(FullImageEncoder, self).__init__()
        self.global_pooling = nn.AvgPool2d(16, stride=16, padding=(8, 8))  # KITTI 16 16
        self.dropout = nn.Dropout2d(p=0.5)
        self.global_fc = nn.Linear(2048 * 4 * 5, 512)  # kitti 4x5
        self.relu = nn.ReLU(inplace=True)
        self.conv1 = nn.Conv2d(512, 512, 1)  # 1x1
        self.upsample = nn.UpsamplingBilinear2d(size=(49, 65))  # KITTI 49X65 NYU 33X45

        weights_init(self.modules(), 'xavier')

    def forward(self, x):
        x1 = self.global_pooling(x)
        x2 = self.dropout(x1)
        x3 = x2.view(-1, 2048 * 4 * 5)  # kitti 4x5
        x4 = self.relu(self.global_fc(x3))
        x4 = x4.view(-1, 512, 1, 1)
        x5 = self.conv1(x4)
        out = self.upsample(x5)
        return out


class SceneUnderstandingModule(nn.Module):
    def __init__(self):
        super(SceneUnderstandingModule, self).__init__()
        self.encoder = FullImageEncoder()
        self.aspp1 = nn.Sequential(
            nn.Conv2d(2048, 512, 1),
            nn.ReLU(inplace=True),
            nn.Conv2d(512, 512, 1),
            nn.ReLU(inplace=True)
        )
        self.aspp2 = nn.Sequential(
            nn.Conv2d(2048, 512, 3, padding=6, dilation=6),
            nn.ReLU(inplace=True),
            nn.Conv2d(512, 512, 1),
            nn.ReLU(inplace=True)
        )
        self.aspp3 = nn.Sequential(
            nn.Conv2d(2048, 512, 3, padding=12, dilation=12),
            nn.ReLU(inplace=True),
            nn.Conv2d(512, 512, 1),
            nn.ReLU(inplace=True)
        )
        self.aspp4 = nn.Sequential(
            nn.Conv2d(2048, 512, 3, padding=18, dilation=18),
            nn.ReLU(inplace=True),
            nn.Conv2d(512, 512, 1),
            nn.ReLU(inplace=True)
        )
        self.concat_process = nn.Sequential(
            nn.Dropout2d(p=0.5),
            nn.Conv2d(512 * 5, 2048, 1),
            nn.ReLU(inplace=True),
            nn.Dropout2d(p=0.5),
            nn.Conv2d(2048, 142, 1),  # KITTI 142 NYU 136 In paper, K = 80 is best, so use 160 is good!
            nn.UpsamplingBilinear2d(size=(385, 513))
        )

        weights_init(self.modules(), type='xavier')

    def forward(self, x):
        x1 = self.encoder(x)

        x2 = self.aspp1(x)
        x3 = self.aspp2(x)
        x4 = self.aspp3(x)
        x5 = self.aspp4(x)

        x6 = torch.cat((x1, x2, x3, x4, x5), dim=1)
        # print('cat x6 size:', x6.size())
        out = self.concat_process(x6)
        return out


class OrdinalRegressionLayer(nn.Module):
    def __init__(self):
        super(OrdinalRegressionLayer, self).__init__()

    def forward(self, x):
        """
        :param x: N X H X W X C, N is batch_size, C is channels of features
        :return: ord_labels is ordinal outputs for each spatial locations , size is N x H X W X C (C = 2K, K is interval of SID)
                 decode_label is the ordinal labels for each position of Image I
        """
        N, C, H, W = x.size()
        ord_num = C // 2

        A = x[:, ::2, :, :].clone()
        B = x[:, 1::2, :, :].clone()

        A = A.view(N, 1, ord_num * H * W)
        B = B.view(N, 1, ord_num * H * W)

        C = torch.cat((A, B), dim=1)
        C = torch.clamp(C, min=1e-8, max=1e8)  # prevent nans

        ord_c = F.softmax(C, dim=1)

        ord_c1 = ord_c[:, 1, :].clone()
        ord_c1 = ord_c1.view(-1, ord_num, H, W)
        decode_c = torch.sum(ord_c1, dim=1).view(-1, 1, H, W)
        return decode_c, ord_c1


class ResNet(nn.Module):
    def __init__(self, in_channels=3, pretrained=True, freeze=True):
        super(ResNet, self).__init__()
        pretrained_model = torchvision.models.__dict__['resnet{}'.format(101)](pretrained=pretrained)

        self.channel = in_channels

        self.conv1 = nn.Sequential(collections.OrderedDict([
            ('conv1_1', nn.Conv2d(self.channel, 64, kernel_size=3, stride=2, padding=1, bias=False)),
            ('bn1_1', nn.BatchNorm2d(64)),
            ('relu1_1', nn.ReLU(inplace=True)),
            ('conv1_2', nn.Conv2d(64, 64, kernel_size=3, stride=1, padding=1, bias=False)),
            ('bn_2', nn.BatchNorm2d(64)),
            ('relu1_2', nn.ReLU(inplace=True)),
            ('conv1_3', nn.Conv2d(64, 128, kernel_size=3, stride=1, padding=1, bias=False)),
            ('bn1_3', nn.BatchNorm2d(128)),
            ('relu1_3', nn.ReLU(inplace=True))
        ]))

        self.maxpool = pretrained_model._modules['maxpool']
        self.layer1 = pretrained_model._modules['layer1']
        self.layer1[0].conv1 = nn.Conv2d(128, 64, kernel_size=(1, 1), stride=(1, 1), bias=False)
        self.layer1[0].downsample[0] = nn.Conv2d(128, 256, kernel_size=(1, 1), stride=(1, 1), bias=False)

        self.layer2 = pretrained_model._modules['layer2']

        self.layer3 = pretrained_model._modules['layer3']
        self.layer3[0].conv2 = nn.Conv2d(256, 256, kernel_size=(3, 3), stride=(1, 1), padding=(1, 1), bias=False)
        self.layer3[0].downsample[0] = nn.Conv2d(512, 1024, kernel_size=(1, 1), stride=(1, 1), bias=False)

        self.layer4 = pretrained_model._modules['layer4']
        self.layer4[0].conv2 = nn.Conv2d(512, 512, kernel_size=(3, 3), stride=(1, 1), padding=(1, 1), bias=False)
        self.layer4[0].downsample[0] = nn.Conv2d(1024, 2048, kernel_size=(1, 1), stride=(1, 1), bias=False)

        # clear memory
        del pretrained_model

        if pretrained:
            weights_init(self.conv1, type='kaiming')
            weights_init(self.layer1[0].conv1, type='kaiming')
            weights_init(self.layer1[0].downsample[0], type='kaiming')
            weights_init(self.layer3[0].conv2, type='kaiming')
            weights_init(self.layer3[0].downsample[0], type='kaiming')
            weights_init(self.layer4[0].conv2, 'kaiming')
            weights_init(self.layer4[0].downsample[0], 'kaiming')
        else:
            weights_init(self.modules(), type='kaiming')

        if freeze:
            self.freeze()

    def forward(self, x):
        x = self.conv1(x)
        x = self.maxpool(x)

        x1 = self.layer1(x)
        x2 = self.layer2(x1)
        x3 = self.layer3(x2)
        x4 = self.layer4(x3)

        return x4

    def freeze(self):
        for m in self.modules():
            if isinstance(m, nn.BatchNorm2d):
                m.eval()


class DORN(nn.Module):
    def __init__(self, output_size=(257, 353), channel=3, pretrained=True, freeze=True):
        super(DORN, self).__init__()

        self.output_size = output_size
        self.channel = channel
        self.feature_extractor = ResNet(in_channels=channel, pretrained=pretrained, freeze=freeze)
        self.aspp_module = SceneUnderstandingModule()
        self.orl = OrdinalRegressionLayer()

    def forward(self, x):
        x1 = self.feature_extractor(x)
        x2 = self.aspp_module(x1)
        depth_labels, ord_labels = self.orl(x2)
        return depth_labels, ord_labels

    def get_1x_lr_params(self):
        b = [self.feature_extractor]
        for i in range(len(b)):
            for k in b[i].parameters():
                if k.requires_grad:
                    yield k

    def get_10x_lr_params(self):
        b = [self.aspp_module, self.orl]
        for j in range(len(b)):
            for k in b[j].parameters():
                if k.requires_grad:
                    yield k
