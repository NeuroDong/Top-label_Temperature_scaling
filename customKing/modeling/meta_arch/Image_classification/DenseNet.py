from multiprocessing import reduction
import re
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.utils.checkpoint as cp
from collections import OrderedDict
from ..build import META_ARCH_REGISTRY

from torch import Tensor
from typing import Any, List, Tuple
from torchvision import transforms


__all__ = ['DenseNet', 'densenet121', 'densenet169', 'densenet201', 'densenet161']

model_urls = {
    'densenet121': 'https://download.pytorch.org/models/densenet121-a639ec97.pth',
    'densenet169': 'https://download.pytorch.org/models/densenet169-b2777c0a.pth',
    'densenet201': 'https://download.pytorch.org/models/densenet201-c1103571.pth',
    'densenet161': 'https://download.pytorch.org/models/densenet161-8d451a50.pth',
}


class _DenseLayer(nn.Module):
    def __init__(
        self,
        num_input_features: int,
        growth_rate: int,
        bn_size: int,
        drop_rate: float,
        reduction:float,
        memory_efficient: bool = False
    ) -> None:
        super(_DenseLayer, self).__init__()

        self.reduction = reduction
        if reduction==0.5:
            self.norm1: nn.BatchNorm2d
            self.add_module('norm1', nn.BatchNorm2d(num_input_features))
            self.relu1: nn.ReLU
            self.add_module('relu1', nn.ReLU(inplace=True))
            self.conv1: nn.Conv2d
            self.add_module('conv1', nn.Conv2d(num_input_features, bn_size *
                                            growth_rate, kernel_size=1, stride=1,
                                            bias=False))
            self.norm2: nn.BatchNorm2d
            self.add_module('norm2', nn.BatchNorm2d(bn_size * growth_rate))
            self.relu2: nn.ReLU
            self.add_module('relu2', nn.ReLU(inplace=True))
            self.conv2: nn.Conv2d
            self.add_module('conv2', nn.Conv2d(bn_size * growth_rate, growth_rate,
                                            kernel_size=3, stride=1, padding=1,
                                            bias=False))
        elif reduction == 1:
            self.norm2: nn.BatchNorm2d
            self.add_module('norm2', nn.BatchNorm2d(num_input_features))
            self.relu2: nn.ReLU
            self.add_module('relu2', nn.ReLU(inplace=True))
            self.conv2: nn.Conv2d
            self.add_module('conv2', nn.Conv2d(num_input_features, growth_rate,
                                            kernel_size=3, stride=1, padding=1,
                                            bias=False))

        self.drop_rate = float(drop_rate)
        self.memory_efficient = memory_efficient

    def bn_function(self, inputs: List[Tensor]) -> Tensor:
        concated_features = torch.cat(inputs, 1)
        bottleneck_output = self.conv1(self.relu1(self.norm1(concated_features)))  # noqa: T484
        return bottleneck_output

    # todo: rewrite when torchscript supports any
    def any_requires_grad(self, input: List[Tensor]) -> bool:
        for tensor in input:
            if tensor.requires_grad:
                return True
        return False

    @torch.jit.unused  # noqa: T484
    def call_checkpoint_bottleneck(self, input: List[Tensor]) -> Tensor:
        def closure(*inputs):
            return self.bn_function(inputs)

        return cp.checkpoint(closure, *input)

    @torch.jit._overload_method  # noqa: F811
    def forward(self, input: List[Tensor]) -> Tensor:
        pass

    @torch.jit._overload_method  # noqa: F811
    def forward(self, input: Tensor) -> Tensor:
        pass

    # torchscript does not yet support *args, so we overload method
    # allowing it to take either a List[Tensor] or single Tensor
    def forward(self, input: Tensor) -> Tensor:  # noqa: F811
        if isinstance(input, Tensor):
            prev_features = [input]
        else:
            prev_features = input

        if self.reduction == 0.5:
            if self.memory_efficient and self.any_requires_grad(prev_features):
                if torch.jit.is_scripting():
                    raise Exception("Memory Efficient not supported in JIT")

                prev_features = self.call_checkpoint_bottleneck(prev_features)
            else:
                prev_features = self.bn_function(prev_features)

        if isinstance(prev_features, Tensor):
            prev_features = [prev_features]
        else:
            prev_features = prev_features

        prev_features = torch.cat(prev_features, dim=1)
        new_features = self.conv2(self.relu2(self.norm2(prev_features)))
        if self.drop_rate > 0:
            new_features = F.dropout(new_features, p=self.drop_rate,
                                     training=self.training)
        return new_features


class _DenseBlock(nn.ModuleDict):
    _version = 2

    def __init__(
        self,
        num_layers: int,
        num_input_features: int,
        bn_size: int,
        growth_rate: int,
        drop_rate: float,
        reduction:float,
        memory_efficient: bool = False
    ) -> None:
        super(_DenseBlock, self).__init__()
        for i in range(num_layers):
            layer = _DenseLayer(
                num_input_features + i * growth_rate,
                growth_rate=growth_rate,
                bn_size=bn_size,
                drop_rate=drop_rate,
                reduction=reduction,
                memory_efficient=memory_efficient,
            )
            self.add_module('denselayer%d' % (i + 1), layer)

    def forward(self, init_features: Tensor) -> Tensor:
        features = [init_features]
        for name, layer in self.items():
            new_features = layer(features)
            features.append(new_features)
        return torch.cat(features, 1)


class _Transition(nn.Sequential):
    def __init__(self, num_input_features: int, num_output_features: int) -> None:
        super(_Transition, self).__init__()
        self.add_module('norm', nn.BatchNorm2d(num_input_features))
        self.add_module('relu', nn.ReLU(inplace=True))
        self.add_module('conv', nn.Conv2d(num_input_features, num_output_features,
                                          kernel_size=1, stride=1, bias=False))
        self.add_module('pool', nn.AvgPool2d(kernel_size=2, stride=2))


class DenseNet(nn.Module):
    r"""Densenet-BC model class, based on
    `"Densely Connected Convolutional Networks" <https://arxiv.org/pdf/1608.06993.pdf>`_.

    Args:
        growth_rate (int) - how many filters to add each layer (`k` in paper)
        block_config (list of 4 ints) - how many layers in each pooling block
        num_init_features (int) - the number of filters to learn in the first convolution layer
        bn_size (int) - multiplicative factor for number of bottle neck layers
          (i.e. bn_size * k features in the bottleneck layer)
        drop_rate (float) - dropout rate after each dense layer
        num_classes (int) - number of classification classes
        memory_efficient (bool) - If True, uses checkpointing. Much more memory efficient,
          but slower. Default: *False*. See `"paper" <https://arxiv.org/pdf/1707.06990.pdf>`_.
    """

    def __init__(
        self,
        growth_rate: int = 32,
        block_config: Tuple[int, int, int, int] = (6, 12, 24, 16),
        num_init_features: int = 64,
        bn_size: int = 4,
        drop_rate: float = 0,
        num_classes: int = 1000,
        reduction = 0.5,
        memory_efficient: bool = False
    ) -> None:

        super(DenseNet, self).__init__()

        # First convolution
        self.features = nn.Sequential(OrderedDict([
            ('conv0', nn.Conv2d(3, num_init_features, kernel_size=7, stride=2,
                                padding=3, bias=False)),
            ('norm0', nn.BatchNorm2d(num_init_features)),
            ('relu0', nn.ReLU(inplace=True)),
            ('pool0', nn.MaxPool2d(kernel_size=3, stride=2, padding=1)),
        ]))

        # Each denseblock
        num_features = num_init_features
        for i, num_layers in enumerate(block_config):
            block = _DenseBlock(
                num_layers=num_layers,
                num_input_features=num_features,
                bn_size=bn_size,
                growth_rate=growth_rate,
                drop_rate=drop_rate,
                reduction=reduction,
                memory_efficient=memory_efficient
            )
            self.features.add_module('denseblock%d' % (i + 1), block)
            num_features = num_features + num_layers * growth_rate
            if i != len(block_config) - 1:
                trans = _Transition(num_input_features=num_features,
                                    num_output_features=int(num_features*reduction))
                self.features.add_module('transition%d' % (i + 1), trans)
                num_features = int(num_features*reduction)

        # Final batch norm
        self.features.add_module('norm5', nn.BatchNorm2d(num_features))

        # Linear layer
        self.classifier = nn.Linear(num_features, num_classes)

        self.lossFun = nn.CrossEntropyLoss()

        # Official init from torch repo.
        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                nn.init.kaiming_normal_(m.weight)
            elif isinstance(m, nn.BatchNorm2d):
                nn.init.constant_(m.weight, 1)
                nn.init.constant_(m.bias, 0)
            elif isinstance(m, nn.Linear):
                nn.init.constant_(m.bias, 0)

    def forward(self, batch_images_tensor,label) -> Tensor:
        features = self.features(batch_images_tensor)
        out = F.relu(features, inplace=True)
        out = F.adaptive_avg_pool2d(out, (1, 1))
        out = torch.flatten(out, 1)
        predict = self.classifier(out)

        if self.training:
            loss = self.lossFun(predict,label)
            return predict,loss
        else:
            return predict

def _densenet(
    arch: str,
    growth_rate: int,
    block_config: Tuple[int, int, int, int],
    num_init_features: int,
    **kwargs: Any
) -> DenseNet:
    model = DenseNet(growth_rate, block_config, num_init_features, **kwargs)
    return model


@META_ARCH_REGISTRY.register()
def densenet_k12_D40(cfg, **kwargs: Any) ->DenseNet:
    model = _densenet('densenet_k12_D40', 12, (12, 12, 12), 16, num_classes = cfg.MODEL.OUTPUT_NUM_ClASSES ,reduction=1,**kwargs)
    model.features.conv0 = nn.Conv2d(3, 16, kernel_size=3, stride=1,padding=1, bias=False)
    model.features.pool0 = nn.Identity()
    return model

@META_ARCH_REGISTRY.register()
def densenet_k12_D100(cfg, **kwargs: Any) ->DenseNet:
    model = _densenet('densenet_k12_D100', 12, (32, 32, 32), 16, num_classes = cfg.MODEL.OUTPUT_NUM_ClASSES,reduction=1,**kwargs)
    model.features.conv0 = nn.Conv2d(3, 16, kernel_size=3, stride=1,padding=1, bias=False)
    model.features.pool0 = nn.Identity()
    return model

@META_ARCH_REGISTRY.register()
def densenet_k24_D100(cfg, **kwargs: Any) ->DenseNet:
    model = _densenet('densenet_k24_D100', 24, (32, 32, 32), 16, num_classes = cfg.MODEL.OUTPUT_NUM_ClASSES,reduction=1,**kwargs)
    model.features.conv0 = nn.Conv2d(3, 16, kernel_size=3, stride=1,padding=1, bias=False)
    model.features.pool0 = nn.Identity()
    return model

@META_ARCH_REGISTRY.register()
def densenet_BC_k12_D100(cfg, **kwargs: Any) ->DenseNet:
    N = int((100 - 4)/(3 * 2))
    model = _densenet('densenet_BC_k12_D100', 12, (N, N, N), 12*2, num_classes = cfg.MODEL.OUTPUT_NUM_ClASSES,reduction=0.5,**kwargs)
    model.features.conv0 = nn.Conv2d(3, 12*2, kernel_size=3, stride=1,padding=1, bias=False)
    model.features.pool0 = nn.Identity()
    return model

@META_ARCH_REGISTRY.register()
def densenet_BC_k24_D250(cfg, **kwargs: Any) ->DenseNet:
    N = int((250 - 4)/(3 * 2))
    model = _densenet('densenet_BC_k24_D250', 24, (N, N, N), 24*2, num_classes = cfg.MODEL.OUTPUT_NUM_ClASSES,reduction=0.5,**kwargs)
    model.features.conv0 = nn.Conv2d(3, 24*2, kernel_size=3, stride=1,padding=1, bias=False)
    model.features.pool0 = nn.Identity()
    return model

@META_ARCH_REGISTRY.register()
def densenet_BC_k40_D190(cfg, **kwargs: Any) ->DenseNet:
    N = int((190 - 4)/(3 * 2))
    model = _densenet('densenet_BC_k40_D190', 40, (N, N, N), 40*2, num_classes = cfg.MODEL.OUTPUT_NUM_ClASSES,reduction=0.5,**kwargs)
    model.features.conv0 = nn.Conv2d(3,40*2, kernel_size=3, stride=1,padding=1, bias=False)
    model.features.pool0 = nn.Identity()
    return model

@META_ARCH_REGISTRY.register()
def densenet121(cfg, **kwargs: Any) -> DenseNet:
    r"""Densenet-121 model from
    `"Densely Connected Convolutional Networks" <https://arxiv.org/pdf/1608.06993.pdf>`_.

    Args:
        pretrained (bool): If True, returns a model pre-trained on ImageNet
        progress (bool): If True, displays a progress bar of the download to stderr
        memory_efficient (bool) - If True, uses checkpointing. Much more memory efficient,
          but slower. Default: *False*. See `"paper" <https://arxiv.org/pdf/1707.06990.pdf>`_.
    """
    return _densenet('densenet121', 32, (6, 12, 24, 16), 64, num_classes = cfg.MODEL.OUTPUT_NUM_ClASSES,
                     **kwargs)

@META_ARCH_REGISTRY.register()
def densenet161(cfg, **kwargs: Any) -> DenseNet:
    r"""Densenet-161 model from
    `"Densely Connected Convolutional Networks" <https://arxiv.org/pdf/1608.06993.pdf>`_.

    Args:
        pretrained (bool): If True, returns a model pre-trained on ImageNet
        progress (bool): If True, displays a progress bar of the download to stderr
        memory_efficient (bool) - If True, uses checkpointing. Much more memory efficient,
          but slower. Default: *False*. See `"paper" <https://arxiv.org/pdf/1707.06990.pdf>`_.
    """
    return _densenet('densenet161', 48, (6, 12, 36, 24), 96, num_classes = cfg.MODEL.OUTPUT_NUM_ClASSES,
                     **kwargs)

@META_ARCH_REGISTRY.register()
def densenet169(cfg, **kwargs: Any) -> DenseNet:
    r"""Densenet-169 model from
    `"Densely Connected Convolutional Networks" <https://arxiv.org/pdf/1608.06993.pdf>`_.

    Args:
        pretrained (bool): If True, returns a model pre-trained on ImageNet
        progress (bool): If True, displays a progress bar of the download to stderr
        memory_efficient (bool) - If True, uses checkpointing. Much more memory efficient,
          but slower. Default: *False*. See `"paper" <https://arxiv.org/pdf/1707.06990.pdf>`_.
    """
    return _densenet('densenet169', 32, (6, 12, 32, 32), 64, num_classes = cfg.MODEL.OUTPUT_NUM_ClASSES,
                     **kwargs)

@META_ARCH_REGISTRY.register()
def densenet201(cfg, **kwargs: Any) -> DenseNet:
    r"""Densenet-201 model from
    `"Densely Connected Convolutional Networks" <https://arxiv.org/pdf/1608.06993.pdf>`_.

    Args:
        pretrained (bool): If True, returns a model pre-trained on ImageNet
        progress (bool): If True, displays a progress bar of the download to stderr
        memory_efficient (bool) - If True, uses checkpointing. Much more memory efficient,
          but slower. Default: *False*. See `"paper" <https://arxiv.org/pdf/1707.06990.pdf>`_.
    """
    return _densenet('densenet201', 32, (6, 12, 48, 32), 64, num_classes = cfg.MODEL.OUTPUT_NUM_ClASSES,
                     **kwargs)