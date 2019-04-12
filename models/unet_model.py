# full assembly of the sub-parts to form the complete net

import torch
from torch import nn, Tensor
from .unet_parts import InConv, DownAndConv, UpAndConv, OutConvMap
from .convnalu import ConvNACCell, ConvNALUCell


class UNet(nn.Module):
    def __init__(self, ch_in, ch_out, ch_base=32):
        super().__init__()
        self.inc = InConv(ch_in, ch_base)

        self.downs = nn.ModuleList(
            [DownAndConv(ch_base * (2**ii), ch_base * (2**(ii + 1)))
             for ii in range(0, 4)]
        )
        self.ups = nn.ModuleList(
            [UpAndConv(ch_base * (2**(ii + 1)), ch_base * (2**ii))
             for ii in reversed(range(0, 4))]
        )

        # self.outc = OutConv(ch_base, ch_out)
        self.outc = OutConvMap(ch_base, ch_out)

    def forward(self, xin):
        x_skip = [Tensor] * 4
        x_skip[0] = self.inc(xin)

        for idx, down in enumerate(self.downs[:-1]):
            x_skip[idx + 1] = down(x_skip[idx])

        x = self.downs[-1](x_skip[-1])

        for item_skip, up in zip(reversed(x_skip), self.ups):
            x = up(x, item_skip)

        x = self.outc(x)
        return x


class UNetNALU(nn.Module):
    def __init__(self, ch_in, ch_out, ch_base=32):
        super().__init__()
        self.inc = InConv(ch_in, ch_base)

        self.downs = nn.ModuleList(
            [DownAndConv(ch_base * (2**ii), ch_base * (2**(ii + 1)))
             for ii in range(0, 4)]
        )
        self.nalu = ConvNACCell(ch_base * (2**4), ch_base * (2**4), (3, 3))
        self.ups = nn.ModuleList(
            [UpAndConv(ch_base * (2**(ii + 1)), ch_base * (2**ii))
             for ii in reversed(range(0, 4))]
        )

        # self.outc = OutConv(ch_base, ch_out)
        self.outc = OutConvMap(ch_base, ch_out)

    def forward(self, xin):
        x_skip = [Tensor] * 4
        x_skip[0] = self.inc(xin)

        for idx, down in enumerate(self.downs[:-1]):
            x_skip[idx + 1] = down(x_skip[idx])

        x = self.downs[-1](x_skip[-1])
        x = self.nalu(x)

        for item_skip, up in zip(reversed(x_skip), self.ups):
            x = up(x, item_skip)

        x = self.outc(x)
        return x