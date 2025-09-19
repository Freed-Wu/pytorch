"""
Definition of CuTe inspired Layouts for DeviceMesh internal bookkeeping and functions to manipulate them
"""

import math
from collections.abc import Iterator
from dataclasses import dataclass
from itertools import product

import torch
from torch.distributed._pycute import (
    coalesce,
    complement,
    composition,
    flatten,
    IntTuple,
    is_int,
    is_tuple,
    Layout,
)


@dataclass(frozen=True, init=True)
class _MeshLayout(Layout):
    """
    Utility class for representing an integer layout by borrowing ideas from CuTe Layout Algebra.
    See https://docs.nvidia.com/cutlass/media/docs/cpp/cute/02_layout_algebra.html for more details.

    Each layout is represented as a list of sizes and strides. We use it as a way for mechanical bookkeeping
    of the integers such as ranks in a SPMD mesh, and the transformation on top of it.

    Lots of methods of layout like coalesce, composition, complement, etc. are borrowed from pycute.
    https://github.com/NVIDIA/cutlass/blob/6dd13d42784ee5bfa232d2441e6b9a021c5c6290/python/pycute/layout.py#L137,L257

    Note this is a CuTe-inspired layout, because CuTe uses co-lexicographic way in linearization while PyTorch
    is using lexicographic. So even though the CuTe documentation can still be referenced, the implementation will be
    different from that of PyCute's.
    """

    shape: IntTuple
    stride: IntTuple

    def __post_init__(self) -> None:
        if not is_tuple(self.shape) and not is_int(self.shape):
            raise TypeError(f"shape must be a tuple or int, got {type(self.shape)}")
        if not is_tuple(self.stride) and not is_int(self.stride):
            raise TypeError(f"stride must be a tuple or int, got {type(self.stride)}")
        if (
            is_tuple(self.shape)
            and is_tuple(self.stride)
            and len(flatten(self.shape)) != len(flatten(self.stride))
        ):
            raise ValueError(
                f"sizes {len(flatten(self.shape))} and "
                f"strides {len(flatten(self.stride))} must have the same length"
            )

    @property
    def sizes(self) -> IntTuple:
        return self.shape

    @property
    def strides(self) -> IntTuple:
        return self.stride

    @property
    def sizes_and_strides(self) -> Iterator[tuple[int, int]]:
        return zip(flatten(self.shape), flatten(self.stride))

    def numel(self) -> int:
        return math.prod(flatten(self.shape))

    # # operator []    (get-i like tuples)
    def __getitem__(self, i: int) -> "_MeshLayout":
        layout = super().__getitem__(i)
        return _MeshLayout(layout.shape, layout.stride)

    def coalesce(self) -> "_MeshLayout":
        """
        A layout is represented by (sizes):(strides), e.g. (3,2):(4,2).
        Two consecutive dimensions can be "merged" into one if their
        strides are contiguous/multiplicative (i.e., the inner stride * inner size
        equals the next stride), we perform this kind of merge inside coalesce.

        Example 1 (simple): (3,2):(2,1)
        - inner dimension: has stride=1, size=2
        - outer dimension: stride = inner_stride * inner_size = 2
        → coalesced = (6:1)    # acts like a flat 1D array of length 6

        Example 2 (non-coalescible): (3,2):(4,1)
        - inner dimension: stride=1, size=2 → 2*1 = 2
        - outer dimension: stride=4, mismatch (≠ 2)
        → cannot merge; result stays (3,2):(4,1)
        """
        layout = coalesce(self)
        return _MeshLayout(layout.shape, layout.stride)

    def composition(self, layout: "_MeshLayout") -> "_MeshLayout":
        """
        By-dimension composition allows one layout to "select from" or "filter through" another layout.
        Think of it as function composition: (self ∘ layout)(input) = self(layout(input))
        between two layouts. This function is a wrapper of pycute's composition.

        Mental model about how to understand the composition logic:
        - The LEFT layout (self) defines the "output space" - what indices are possible
        - The RIGHT layout (layout parameter) acts as a "selector" - which specific indices to pick
        - The composition only generates indices that the left layout could originally produce,
          but the right layout determines which indices to be picked.
        - The stride of the composition layout will not be smaller than the stride of the right layout,
          because when picking the indices the composition will at least follow the the right layout's stride
          to move forward.

        Example:
          self = (6,2):(2,1)      # sizes=(6,2), strides=(2,1)
          layout = (3:2)          # sizes=(3,), stride=(2,)
          self o layout = (3:2)

        Returns:
          Layout being composed.
        """
        result = composition(self, layout)
        return _MeshLayout(result.shape, result.stride)

    def complement(self, world_size: int) -> "_MeshLayout":
        """
        Compute the "complement layout" relative to a given world_size.
        A complement layout fills in the "missing" factor so that: self repeat a layout of complement(self, world_size)
        will get a complete world_size. We use ⊗ to denote the repeat operation.

        Example:
          self = (4:1)   # size=4, stride=1
          world_size = 8
          Then:
            complete needed factor = 8 / 4 = 2
            complement(self, 8) = (2:1)

          Together they form:
            (4:1) ⊗ (2:1) = (4,2):(2,1)
          which has world_size = 4 * 2 = 8, as required.

        In distributed terms, complement() is often used to derive the "other"
        rank grouping when splitting processes into 2D meshes.

        For a visualized explanation, see https://x.com/ezyang/status/1962364978393981433/
        """
        layout = complement(self, world_size)
        return _MeshLayout(layout.shape, layout.stride)

    def member_ranks(self) -> list[int]:
        """
        This function computes the all ranks specified by the layout.

        How it works:
        1. we enumerates every possible coordinate (like a nested for-loop).
        If sizes = (2, 3), we get the following coordinates:
            (0,0), (0,1), (0,2), (1,0), (1,1), (1,2)

        2. For each coordinate, we compute a linear rank index as:
            member_ranks = sum(coord[i] * strides[i] for i in range(ndim))

        Example A:
        sizes = (2, 3)        # 2 rows, 3 cols
        strides = (3, 1)        # row-major layout
        coords = (0,0) -> 0*3 + 0*1 = 0
                 (0,1) -> 0*3 + 1*1 = 1
                 (0,2) -> 0*3 + 2*1 = 2
                 (1,0) -> 1*3 + 0*1 = 3
                 (1,1) -> 1*3 + 1*1 = 4
                 (1,2) -> 1*3 + 2*1 = 5
        result = [0, 1, 2, 3, 4, 5]

        Example B:
        sizes = (2, 3)
        strides = (1, 2)        # non-standard / strided layout
        coords = (0,0) -> 0*1 + 0*2 = 0
                 (0,1) -> 0*1 + 1*2 = 2
                 (0,2) -> 0*1 + 2*2 = 4
                 (1,0) -> 1*1 + 0*2 = 1
                 (1,1) -> 1*1 + 1*2 = 3
                 (1,2) -> 1*1 + 2*2 = 5
        result = [0, 2, 4, 1, 3, 5]
        """
        return [
            sum(c * s for c, s in zip(coord, flatten(self.strides)))
            for coord in product(*(range(s) for s in flatten(self.sizes)))
        ]

    def global_ranks(self, world_size: int) -> list[list[int]]:
        """
        Build global ranks specified by the layout via two-level ranks composition.

        The nested list forms the Cartesian product of group ranks and group offset
        and the final global ranks are the addition of these two. The result is a
        list of lists: one sublist per group. This rank list will be used to build
        the communicator underlying the layout.

        Example:
        world_size = 16
        self.size = 4
        self.stride = 1
        group ranks = [0, 1, 2, 3]
        group offsets = [0, 4, 8, 12]
        result = [
            [0+0, 0+1, 0+2, 0+3],  # → [0, 1, 2, 3]
            [4+0, 4+1, 4+2, 4+3],  # → [4, 5, 6, 7]
            [8+0, 8+1, 8+2, 8+3],  # → [8, 9, 10,11]
            [12+0, 12+1, 12+2, 12+3],  # → [12,13,14,15]
        ]
        """
        return [
            [group_offset + group_rank for group_rank in self.member_ranks()]
            for group_offset in self.complement(world_size).member_ranks()
        ]

    def check_non_overlap(self) -> bool:
        """
        Check if the layout has any overlap between the ranks it generates. If there is overlap,
        we return False, otherwise True.

        Aside from indice 0, indices from each dim of the layout must be non-overlapping.

        Here is how it works:
        1. Sort dimensions by stride (smallest stride first)
        2. For each dimension, check if:
           - It has the same stride as previous dimension (duplicate mapping)
           - Its stride overlaps with the previous dimension's span

        A dimension's "span" is size * stride, representing the address space it covers.

        Example 1 - Valid (no overlap):
        Layout: sizes=(2,3), strides=(6,1)
        - Dim 1: stride=1, span=3*1=3, covers addresses [0,1,2]
        - Dim 0: stride=6, span=2*6=12, covers addresses [0,6]
        → No overlap since 6 > 3

        Example 2 - Invalid (overlap):
        Layout: sizes=(2,3), strides=(2,1)
        - Dim 1: stride=1, span=3*1=3, covers addresses [0,1,2]
        - Dim 0: stride=2, span=2*2=4, covers addresses [0,2]
        → Overlap! stride=2 < span=3, so addresses [0,2] are duplicated

        Returns:
            bool: True if no overlap exists (valid layout), False if overlap detected
        """
        previous_span = -1
        previous_stride = -1
        for size, stride in sorted(self.sizes_and_strides, key=lambda x: x[1]):
            if size == 1:
                continue
            if previous_stride == stride or stride < previous_span:
                return False
            previous_stride = stride
            previous_span = size * stride
        return True

    def to_remapping_tensor(
        self,
        original_mesh_tensor: torch.Tensor,
        world_size: int,
    ) -> torch.Tensor:
        """
        Convert this layout into a tensor representation that maps the logical mesh
        structure to actual device ranks, handling cases where the mesh doesn't use
        consecutive ranks or doesn't span the full world size (Neither is CuTe representible).

        With this method, the cute layout serves as the backend of indices bookkeeping for the
        mesh tensor when it comes to flatten, unflatten and slicing operations. The actual mesh
        tensor still represents the actual device assignment and ranks. We need this function
        to specify device allocation and create backend for a mesh.

        Overview:
        1. Generate logical process groups using this layout's structure
        2. Check if the original mesh uses consecutive ranks (0,1,2,...)
        3. If consecutive: return the logical groups directly
        4. If non-consecutive or partial world: map logical indices to actual ranks

        Examples:

        Case 1 - Consecutive ranks, full world:
        original_mesh_tensor = [[0,1],[2,3]]  # 2x2 mesh, ranks 0-3
        world_size = 4
        layout = Layout(2:2)
        → Returns logical groups directly: [[0,2],[1,3]]

        Case 2 - Non-consecutive ranks:
        original_mesh_tensor = [[10,20],[30,40]]  # custom rank assignment
        world_size = 4
        layout = Layout(2:2)
        → Maps logical indices to actual ranks: [[[10,30],[20,40]]]

        Case 3 - Partial world (stride scaling needed):
        original_mesh_tensor = [[0,1]]  # 1x2 mesh in world_size=8
        world_size = 8
        layout = Layout((2,), (4,))  # every 4th rank
        → Scale down stride: (4,) → (1,) to fit mesh size
        → Map scaled indices to actual ranks: [[0,1]]

        Args:
            original_mesh_tensor: The concrete mesh tensor with actual device ranks
            world_size: Total number of ranks in the distributed system

        Returns:
            torch.Tensor: A tensor representing the actual device rank from original_mesh_tensor
        """

        def scale_stride(scale: int, strides: IntTuple) -> IntTuple:
            """
            Recursively scale down strides by a factor to fit within smaller mesh.

            When layout expects world_size=8 but mesh only has 4 elements,
            we need to scale strides down by factor of 2 to generate valid indices.

            Example: stride=4 with scale=2 → stride=2 (or keep as-is if stride < scale)
            """
            if is_int(strides):
                return strides if strides < scale else strides // scale
            else:
                return tuple(scale_stride(scale, stride) for stride in strides)

        # Create tensor representation of the mesh
        pg_ranks_by_dim = self.global_ranks(original_mesh_tensor.numel())
        sizes = flatten(self.sizes)
        tensor = torch.tensor(pg_ranks_by_dim, device="cpu", dtype=torch.int).view(
            -1,
            *sizes,  # type: ignore[arg-type]
        )

        # When the mesh tensor value can be represented as a cute layout, we can use the global ranks
        # generated by the layout directly for the mesh tensor. Otherwise, the ranks generated by the layout
        # will be used as indices to get the actual ranks from the original mesh tensor.
        if torch.equal(
            original_mesh_tensor.flatten().sort().values,
            torch.arange(
                original_mesh_tensor.numel(),
                device=original_mesh_tensor.device,
                dtype=original_mesh_tensor.dtype,
            ),
        ):
            return tensor

        # This is important because the indices generated by the layout will be larger than the original mesh tensor
        # when the original mesh tensor does not contain all ranks in the world. So we need to scale the layout's stride
        # by world_size // mesh_tensor.numel() so that the indices generated by the layout will be within the range of
        # the original mesh tensor.
        if original_mesh_tensor.numel() != world_size:
            scale_factor = world_size // original_mesh_tensor.numel()
            scaled_strides = scale_stride(scale_factor, self.strides)
            scaled_layout = _MeshLayout(self.sizes, scaled_strides)
            pg_ranks_by_dim = scaled_layout.global_ranks(original_mesh_tensor.numel())
            tensor = torch.tensor(pg_ranks_by_dim, device="cpu", dtype=torch.int).view(
                -1,
                *sizes,  # type: ignore[arg-type]
            )
        return original_mesh_tensor.flatten()[tensor]
