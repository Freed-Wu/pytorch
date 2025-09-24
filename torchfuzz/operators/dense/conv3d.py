"""3D convolution operator implementation."""

import random
from ..base import Operator
from torchfuzz.tensor import Tensor


class Conv3dOperator(Operator):
    """Operator for 3D convolution (torch.nn.functional.conv3d)."""

    def __init__(self):
        super().__init__("conv3d")

    def can_produce(self, tensor):
        """Conv3d can produce 5D tensors (batch, out_channels, depth, height, width)."""
        return len(tensor.size) == 5

    def decompose(self, tensor):
        """Decompose tensor into input tensors for conv3d operation."""
        # tensor shape is (batch_size, out_channels, out_depth, out_height, out_width)
        batch_size, out_channels, out_depth, out_height, out_width = tensor.size

        # Choose input parameters to ensure exact output dimensions
        in_channels = random.choice([64, 128, 256, 512])
        kernel_size = random.choice([1, 3])  # Use smaller kernels for 3D to avoid memory issues
        stride = 1  # Use stride=1 for simplicity

        # Calculate input dimensions that will produce exact output dimensions
        # For stride=1: out_dim = in_dim + 2 * padding - kernel_size + 1
        # We'll choose padding=0 for simplicity and calculate in_dim
        padding = 0

        # Solve for in_dim: in_dim = out_dim - 2 * padding + kernel_size - 1
        in_depth = out_depth - 2 * padding + kernel_size - 1
        in_height = out_height - 2 * padding + kernel_size - 1
        in_width = out_width - 2 * padding + kernel_size - 1
        in_depth = max(in_depth, kernel_size)  # Ensure valid input size
        in_height = max(in_height, kernel_size)
        in_width = max(in_width, kernel_size)

        # Verify the calculation works
        calculated_out_depth = (in_depth + 2 * padding - kernel_size) // stride + 1
        calculated_out_height = (in_height + 2 * padding - kernel_size) // stride + 1
        calculated_out_width = (in_width + 2 * padding - kernel_size) // stride + 1
        if (calculated_out_depth != out_depth or
            calculated_out_height != out_height or
            calculated_out_width != out_width):
            # If it doesn't work, use kernel_size=1 and padding=0 for guaranteed match
            kernel_size = 1
            padding = 0
            in_depth = out_depth    # For kernel=1, stride=1, padding=0: out_dim = in_dim
            in_height = out_height
            in_width = out_width

        # Input tensor: (batch_size, in_channels, in_depth, in_height, in_width)
        input_size = (batch_size, in_channels, in_depth, in_height, in_width)

        # Weight tensor: (out_channels, in_channels, kernel_size, kernel_size, kernel_size)
        weight_size = (out_channels, in_channels, kernel_size, kernel_size, kernel_size)

        # Calculate strides for contiguous tensors
        def calc_stride(size):
            stride = [1]
            for dim in reversed(size[:-1]):
                stride.insert(0, stride[0] * dim)
            return tuple(stride)

        input_stride = calc_stride(input_size)
        weight_stride = calc_stride(weight_size)

        # Type promotion for realistic types
        dtype = tensor.dtype

        # Create input tensors
        input_tensor = Tensor(input_size, input_stride, dtype, tensor.device, tensor.supported_ops)
        weight_tensor = Tensor(weight_size, weight_stride, dtype, tensor.device, tensor.supported_ops)

        result = [input_tensor, weight_tensor]

        # Store parameters for codegen
        self._stride = stride
        self._padding = padding
        return result

    def codegen(self, output_name, input_names, output_tensor):
        """Generate code for conv3d operation."""
        stride = getattr(self, '_stride', 1)
        padding = getattr(self, '_padding', 0)

        return f"{output_name} = torch.nn.functional.conv3d({input_names[0]}, {input_names[1]}, stride={stride}, padding={padding})"
