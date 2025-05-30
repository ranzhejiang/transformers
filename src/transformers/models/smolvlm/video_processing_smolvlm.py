# coding=utf-8
# Copyright 2025 The HuggingFace Inc. team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


from typing import List, Optional, Union

import numpy as np

from ...image_processing_utils import (
    BatchFeature,
)
from ...image_utils import (
    IMAGENET_STANDARD_MEAN,
    IMAGENET_STANDARD_STD,
    SizeDict,
)
from ...processing_utils import Unpack, VideosKwargs
from ...utils import (
    TensorType,
    is_torch_available,
    is_torchvision_available,
    is_torchvision_v2_available,
    is_vision_available,
)
from ...utils.import_utils import requires
from ...video_processing_utils import (
    BaseVideoProcessor,
)
from ...video_utils import group_videos_by_shape, reorder_videos


if is_vision_available():
    from ...image_utils import PILImageResampling

if is_torchvision_available():
    if is_torchvision_v2_available():
        from torchvision.transforms.v2 import functional as F
    else:
        from torchvision.transforms import functional as F


if is_torch_available():
    import torch

from ...utils import logging


logger = logging.get_logger(__name__)

DEFAULT_SYSTEM_MESSAGE = "You are a helpful language and vision assistant. You are able to understand the visual content that the user provides, and assist the user with a variety of tasks using natural language."
DEFAULT_VIDEO_INTRO = (
    "You are provided the following series of {frame_count} frames from a {video_duration} [H:MM:SS] video.\n"
)
DEFAULT_MEDIA_OUTTRO = "\n\n"
FRAME_TIMESTAMP_MESSAGE = "\nFrame from {timestamp}:"
MAX_IMAGE_SIZE = 4096  # 4k resolution as absolute maximum


def smolvlm_sample_indices_fn(metadata, max_frames, target_fps, skip_secs=0):
    """
    Example sampling function which:
      - Uses `max_frames` (if provided) or calculates it from `fps` and metadata.
      - Applies a basic center-skip if fewer frames than available, otherwise
        optionally skips `skip_secs` from both the start and end.
      - Uniformly samples the desired number of frames between the start and end indices.

    Args:
        max_frames (`int`):
            Maximum number of frames to sample.
        target_fps (`int`):
            Target frames to sample per second.
        metadata (`dict`):
            Contains video metadata such as "n_frames" and "video_fps".
        skip_secs (`float`, *optional*, defaults to 1.0):
            Number of seconds to skip from the start and end if the video is long enough.

    Returns:
        numpy.ndarray:
            An array of unique frame indices to sample.
    """

    total_num_frames = getattr(metadata, "total_num_frames", 0)
    if total_num_frames <= 0:
        raise ValueError(f"Invalid total_num_frames={total_num_frames} in metadata.")

    native_fps = getattr(metadata, "fps", 30.0)
    duration_seconds = getattr(metadata, "duration", 0)

    if duration_seconds <= 0:
        raise ValueError(f"Invalid duration_seconds={duration_seconds} in metadata.")

    # Step 1) Estimate how many frames we'd sample at `target_fps`, fallback if target_fps <= 0
    estimated_frames = int(round(target_fps * duration_seconds))

    # Step 2) desired_frames
    desired_frames = min(estimated_frames, max_frames)
    if desired_frames < 1:
        desired_frames = 1

    # Step 3) center skip logic
    start_idx = 0
    end_idx = total_num_frames - 1

    if skip_secs > 0 and (duration_seconds - 2 * skip_secs) > (max_frames * target_fps):
        start_idx = int(skip_secs * native_fps)
        end_idx = int(total_num_frames - skip_secs * native_fps)

    start_idx = max(0, start_idx)
    end_idx = min(end_idx, total_num_frames - 1)
    if start_idx >= end_idx:
        start_idx, end_idx = 0, total_num_frames - 1

    indices = np.linspace(start_idx, end_idx, desired_frames, dtype=int)
    indices = np.unique(indices)

    return indices


def get_max_height_width(videos: list["torch.Tensor"]) -> List[int]:
    """
    Get the maximum height and width across all videos in a batch.
    """
    max_height = max_width = float("-inf")
    for video in videos:
        height, width = video.size()[-2:]
        max_height = max(height, max_height)
        max_width = max(width, max_width)
    return (max_height, max_width)


def get_resize_output_image_size(
    video,
    resolution_max_side: int,
) -> tuple[int, int]:
    """
    Get the output size of the video after resizing given a dictionary specifying the max and min sizes.
    Args:
        video (`np.ndarray`):
            Video to resize.
        resolution_max_side (`int`):
            The longest edge of the video will be resized to this value. The shortest edge will be resized to keep the
            input aspect ratio.
    Returns:
        The output size of the video after resizing.
    """
    height, width = video.size()[-2:]

    # Find the output size, when rescaling the longest edge to max_len and preserving the aspect ratio
    # The output size must be below the MAX_IMAGE_SIZE
    resolution_max_side = min(MAX_IMAGE_SIZE, resolution_max_side)
    resolution_max_side = max(height, width) if resolution_max_side is None else resolution_max_side
    aspect_ratio = width / height

    if width >= height:
        width = resolution_max_side
        height = int(width / aspect_ratio)
        if height % 2 != 0:
            height += 1
    elif height > width:
        height = resolution_max_side
        width = int(height * aspect_ratio)
        if width % 2 != 0:
            width += 1

    height = max(height, 1)
    width = max(width, 1)

    return height, width


class SmolVLMVideoProcessorInitKwargs(VideosKwargs): ...


@requires(backends=("torchvision",))
class SmolVLMVideoProcessor(BaseVideoProcessor):
    resample = PILImageResampling.LANCZOS
    size = {"longest_edge": 4 * 364}
    image_mean = IMAGENET_STANDARD_MEAN
    image_std = IMAGENET_STANDARD_STD
    do_resize = True
    do_rescale = True
    do_normalize = True
    do_convert_rgb = True
    do_pad = True
    valid_kwargs = SmolVLMVideoProcessorInitKwargs
    model_input_names = ["pixel_values", "pixel_attention_mask"]

    def __init__(self, **kwargs: Unpack[SmolVLMVideoProcessorInitKwargs]):
        super().__init__(**kwargs)

    def resize(
        self,
        video: "torch.Tensor",
        size: SizeDict,
        interpolation: "F.InterpolationMode" = None,
        antialias: bool = True,
        **kwargs,
    ) -> "torch.Tensor":
        """
        Resize an video to `(size["height"], size["width"])`.
        Args:
            video (`torch.Tensor`):
                Video to resize.
            size (`SizeDict`):
                Dictionary in the format `{"height": int, "width": int}` specifying the size of the output video.
            resample (`InterpolationMode`, *optional*, defaults to `InterpolationMode.BILINEAR`):
                `InterpolationMode` filter to use when resizing the video e.g. `InterpolationMode.BICUBIC`.
        Returns:
            `torch.Tensor`: The resized video.
        """
        interpolation = interpolation if interpolation is not None else F.InterpolationMode.BILINEAR
        if interpolation == F.InterpolationMode.LANCZOS:
            logger.warning_once(
                "You have used fast image processor with LANCZOS resample which not yet supported for torch.Tensor. "
                "BICUBIC resample will be used as an alternative. Please fall back to image processor if you "
                "want full consistency with the original model."
            )
            interpolation = F.InterpolationMode.BICUBIC

        if size.longest_edge:
            # Resize the image so that the shortest edge or the longest edge is of the given size
            # while maintaining the aspect ratio of the original image.
            new_size = get_resize_output_image_size(
                video,
                resolution_max_side=size.longest_edge,
            )
        elif size.height and size.width:
            new_size = (size.height, size.width)
        else:
            raise ValueError(f"Size must contain 'height' and 'width' keys, or 'longest_edge' key. Got {size}.")
        return F.resize(video, new_size, interpolation=interpolation, antialias=antialias)

    def pad(
        self,
        video: "torch.Tensor",
        padded_size: tuple[int, int],
        fill: int = 0,
        return_pixel_mask: bool = True,
    ):
        """Pads the sample with empty video to the padded_size
        Args:
            video (`torch.Tensor`):
                Video to pad.
            padded_size (`Tuple[int, int]`):
                Height and width to pad.
            fill (`int`, *optional*):
                The value to use for the padding.
            return_pixel_mask (`bool`, *optional*, defaults to `True`):
                Whether to return a pixel mask.
        """
        original_size = video.size()[-2:]
        padding_bottom = padded_size[0] - original_size[0]
        padding_right = padded_size[1] - original_size[1]
        if padding_bottom < 0 or padding_right < 0:
            raise ValueError(
                f"Padding dimensions are negative. Please make sure that the padded size is larger than the "
                f"original size. Got padded size: {padded_size}, original size: {original_size}."
            )
        if original_size != padded_size:
            padding = [0, 0, padding_right, padding_bottom]
            video = F.pad(video, padding, fill=fill)

        # Make a pixel mask for the video, where 1 indicates a valid pixel and 0 indicates padding.
        pixel_mask = None
        if return_pixel_mask:
            pixel_mask = torch.zeros_like(video[..., 0, :, :], dtype=torch.int64)
            pixel_mask[..., : original_size[0], : original_size[1]] = 1

        return video, pixel_mask

    def _preprocess(
        self,
        videos: List["torch.Tensor"],
        do_convert_rgb: bool,
        do_resize: bool,
        size: SizeDict,
        interpolation: Optional["F.InterpolationMode"],
        do_rescale: bool,
        rescale_factor: float,
        do_normalize: bool,
        do_pad: bool,
        image_mean: Optional[Union[float, List[float]]],
        image_std: Optional[Union[float, List[float]]],
        return_tensors: Optional[Union[str, TensorType]] = None,
        **kwargs,
    ):
        # Group videos by size for batched resizing
        grouped_videos, grouped_videos_index = group_videos_by_shape(videos)
        resized_videos_grouped = {}
        for shape, stacked_videos in grouped_videos.items():
            if do_convert_rgb:
                stacked_videos = self.convert_to_rgb(stacked_videos)
            if do_resize:
                stacked_videos = self.resize(stacked_videos, size=size, interpolation=interpolation)
            resized_videos_grouped[shape] = stacked_videos
        resized_videos = reorder_videos(resized_videos_grouped, grouped_videos_index)

        grouped_videos, grouped_videos_index = group_videos_by_shape(resized_videos)
        processed_videos_grouped = {}
        for shape, stacked_videos in grouped_videos.items():
            stacked_videos = self.rescale_and_normalize(
                stacked_videos, do_rescale, rescale_factor, do_normalize, image_mean, image_std
            )
            processed_videos_grouped[shape] = stacked_videos

        processed_videos = reorder_videos(processed_videos_grouped, grouped_videos_index)

        if do_pad:
            pad_size = get_max_height_width(processed_videos)
            grouped_videos, grouped_videos_index = group_videos_by_shape(processed_videos)
            processed_padded_mask_grouped = {}
            processed_videos_grouped = {}

            for shape, stacked_videos in grouped_videos.items():
                stacked_videos, padded_masks = self.pad(stacked_videos, padded_size=pad_size)
                processed_videos_grouped[shape] = stacked_videos
                processed_padded_mask_grouped[shape] = padded_masks

            processed_videos = reorder_videos(processed_videos_grouped, grouped_videos_index)
            pixel_attention_mask = reorder_videos(processed_padded_mask_grouped, grouped_videos_index)

        processed_videos = torch.stack(processed_videos, dim=0) if return_tensors else processed_videos
        data = {"pixel_values": processed_videos}

        if do_pad:
            data["pixel_attention_mask"] = (
                torch.stack(pixel_attention_mask, dim=0)
                if do_pad and return_tensors is not None
                else pixel_attention_mask
            )
        return BatchFeature(data, tensor_type=return_tensors)


__all__ = ["SmolVLMVideoProcessor"]
