import json
import sys
import re
from enum import Enum, auto
from importlib import resources
from pathlib import Path
from typing import Sequence

import magic
from PIL import Image, ImageDraw, ImageFont, ImageEnhance
from attrs import frozen, define
from loguru import logger

import pcleaner.config as cfg
import pcleaner.data

# If using Python 3.10 or older, use the 3rd party StrEnum.
if sys.version_info < (3, 11):
    from strenum import StrEnum
else:
    from enum import StrEnum


class DetectedLang(StrEnum):
    JA = "ja"
    ENG = "eng"
    UNKNOWN = "unknown"

    def __str__(self):
        return self.value


class BoxType(Enum):
    BOX = 0
    EXTENDED_BOX = 1
    MERGED_EXT_BOX = 2
    REFERENCE_BOX = 3


@frozen
class Box:
    x1: int
    y1: int
    x2: int
    y2: int

    # You can create a box from a tuple of (x1, y1, x2, y2) using the * operator:
    # Box(*box_tuple)

    @property
    def as_tuple(self) -> tuple[int, int, int, int]:
        return self.x1, self.y1, self.x2, self.y2

    @property
    def as_tuple_xywh(self) -> tuple[int, int, int, int]:
        # QRect expects (x1, y1, width, height)
        return self.x1, self.y1, self.x2 - self.x1, self.y2 - self.y1

    def __str__(self) -> str:
        """
        String representation of the box coordinates, basically unpacking the tuple.

        :returns: The box coordinates as a string.
        """
        return f"{self.x1},{self.y1},{self.x2},{self.y2}"

    def __contains__(self, point: tuple[int, int]) -> bool:
        """
        Check if a point is inside the box.

        :param point: The point to check.
        :returns: True if the point is inside the box, False otherwise.
        """
        x, y = point
        return self.x1 <= x <= self.x2 and self.y1 <= y <= self.y2

    @property
    def area(self) -> int:
        return (self.x2 - self.x1) * (self.y2 - self.y1)

    @property
    def center(self) -> tuple[int, int]:
        return (self.x1 + self.x2) // 2, (self.y1 + self.y2) // 2

    def merge(self, box: "Box") -> "Box":
        x_min = min(self.x1, box.x1)
        y_min = min(self.y1, box.y1)
        x_max = max(self.x2, box.x2)
        y_max = max(self.y2, box.y2)

        return Box(x_min, y_min, x_max, y_max)

    def overlaps(self, other: "Box", threshold: float) -> bool:
        """
        Check if this box overlaps with another box.
        Merge the boxes if more than this percentage (0-100) of the smaller box is covered by the larger box.

        :param other: The other box to check for overlap.
        :param threshold: The threshold for the overlap check.
        :return: True if the boxes overlap, False otherwise.
        """
        # Calculate the area of the intersection.
        x_overlap = max(0, min(self.x2, other.x2) - max(self.x1, other.x1))
        y_overlap = max(0, min(self.y2, other.y2) - max(self.y1, other.y1))
        intersection = x_overlap * y_overlap
        # Get the area of the smaller box. Ensure this can never be 0.
        smaller_area = min(self.area, other.area) or 1

        return intersection / smaller_area > (threshold / 100)

    def overlaps_center(self, other: "Box") -> bool:
        return self.center in other or other.center in self

    def pad(self, amount: int, canvas_size: tuple[int, int]) -> "Box":
        """
        Grow the box by amount pixels, respecting the canvas size.

        :param amount: The amount of pixels to grow the box by.
        :param canvas_size: The size of the canvas to respect.
        """
        x1_new = max(self.x1 - amount, 0)
        y1_new = max(self.y1 - amount, 0)
        x2_new = min(self.x2 + amount, canvas_size[0])
        y2_new = min(self.y2 + amount, canvas_size[1])

        return Box(x1_new, y1_new, x2_new, y2_new)

    def right_pad(self, amount: int, canvas_size: tuple[int, int]) -> "Box":
        """
        Right-pad the box by amount pixels, respecting the canvas size.

        :param amount: The amount of pixels to right-pad the box by.
        :param canvas_size: The size of the canvas to respect.
        """
        x2_new = min(self.x2 + amount, canvas_size[0])

        return Box(self.x1, self.y1, x2_new, self.y2)

    def scale(self, factor: float) -> "Box":
        """
        Scale the box by a factor.

        :param factor: The factor to scale the box by.
        :return: The scaled box.
        """
        x1_new = int(self.x1 * factor)
        y1_new = int(self.y1 * factor)
        x2_new = int(self.x2 * factor)
        y2_new = int(self.y2 * factor)

        return Box(x1_new, y1_new, x2_new, y2_new)


@define
class PageData:
    """
    This dataclass represents the json data generated by the ai model.
    It contains the image path, mask path, the boxes, the extended boxes,
    and the merged extended boxes.

    - boxes: The boxes generated by the ai model, only expanded slightly for extra padding.
      These are used to make a tight-fitting mask.
    - extended_boxes: The boxes expanded a lot and used for masking off potential false positives.
    - merged_extended_boxes: Overlapping extended boxes are merged into one box, to prevent
      conflicts of overlapping mask regions. The original extended boxes are still kept,
      to provide a more precise mask for the initial cut. These are used to cut out the mask when
      analyzing the fit.
    - reference_boxes: These are extensions of the merged extended boxes, to make sure the mask
      has room to grow in the base image when providing analysis. These are used to cut out the
      base image when analyzing the fit.

    Boxes are represented as tuples of (x1, y1, x2, y2), where (x1, y1) is the top left corner,
    and (x2, y2) is the bottom right corner.
    """

    image_path: str  # Path to the copied png.
    mask_path: str  # Path to the generated mask.png
    original_path: str  # Path to the original image. (used for relative output)
    scale: float  # The size of the original image relative to the png.
    boxes: list[Box]
    extended_boxes: list[Box]
    merged_extended_boxes: list[Box]
    reference_boxes: list[Box]
    _image_size: tuple[int, int] = (
        None  # Cache the image size, so we don't have to load the image every time.
    )

    @classmethod
    def from_json(cls, json_str: str) -> "PageData":
        """
        Load a previously dumped PageData object from a json file.

        :param json_str: The json string to load from.
        """
        json_data = json.loads(json_str)
        return cls(
            json_data["image_path"],
            json_data["mask_path"],
            json_data["original_path"],
            json_data["scale"],
            [Box(*b) for b in json_data["boxes"]],
            [Box(*b) for b in json_data["extended_boxes"]],
            [Box(*b) for b in json_data["merged_extended_boxes"]],
            [Box(*b) for b in json_data["reference_boxes"]],
        )

    def to_json(self) -> str:
        """
        Dump the PageData object to a json string.
        We want to exclude the _image_size attribute, since it's not needed.
        Box classes need to be serialized as tuples.
        """
        data = {
            "image_path": self.image_path,
            "mask_path": self.mask_path,
            "original_path": self.original_path,
            "scale": self.scale,
            "boxes": [b.as_tuple for b in self.boxes],
            "extended_boxes": [b.as_tuple for b in self.extended_boxes],
            "merged_extended_boxes": [b.as_tuple for b in self.merged_extended_boxes],
            "reference_boxes": [b.as_tuple for b in self.reference_boxes],
        }
        return json.dumps(data, indent=4)

    @property
    def image_size(self) -> tuple[int, int]:
        if self._image_size is None:
            try:
                metadata = magic.from_file(self.image_path)
                size_str = re.search(r"(\d+) x (\d+)", metadata).groups()
                self._image_size = (int(size_str[0]), int(size_str[1]))
            except (UnicodeDecodeError, AttributeError):
                # Unicode error: Windows is up to some bullshit again.
                # Attribute error: magic can't deal with windows' bullshit either, returning "cannot open".
                # Something got fucked, time for plan B.
                logger.error(
                    f"Encountered a Unicode Error for file '{self.image_path}', using fallback method."
                )
                temp_image = Image.open(self.image_path)
                self._image_size = temp_image.size

        return self._image_size

    def boxes_from_type(self, box_type: BoxType) -> list[Box]:
        match box_type:
            case BoxType.BOX:
                return self.boxes
            case BoxType.EXTENDED_BOX:
                return self.extended_boxes
            case BoxType.MERGED_EXT_BOX:
                return self.merged_extended_boxes
            case BoxType.REFERENCE_BOX:
                return self.reference_boxes
            case _:
                raise ValueError("Invalid box type.")

    def grow_boxes(self, padding: int, box_type: BoxType) -> None:
        """
        Uniformly grow all boxes by padding pixels.
        Checks that the boxes don't go out of bounds.

        :param padding: number of pixels to grow each box by.
        :param box_type: type of box to grow.
        """
        boxes = self.boxes_from_type(box_type)
        for i, box in enumerate(boxes):
            boxes[i] = box.pad(padding, self.image_size)

    def right_pad_boxes(self, padding: int, box_type: BoxType) -> None:
        """
        Right-pad all boxes by padding pixels.
        Checks that the boxes don't go out of bounds.

        :param padding: number of pixels to right-pad each box by.
        :param box_type: type of box to right-pad.
        """
        boxes = self.boxes_from_type(box_type)
        for i, box in enumerate(boxes):
            boxes[i] = box.right_pad(padding, self.image_size)

    def visualize(self, image_path: Path | str, output_path: Path | str) -> None:
        """
        Visualize the boxes on an image.
        Typically, this would be used to check where on the original image the
        boxes are located.

        :param image_path: The path to the image to visualize the boxes on.
        :param output_path: A full file path to write the image to.
        """
        image = Image.open(image_path)
        with resources.files(pcleaner.data) as data_path:
            font_path = str(data_path / "LiberationSans-Regular.ttf")
        logger.debug(f"Loading included font from {font_path}")
        # Figure out the optimal font size based on the image size. E.g. 30 for a 1600px image.
        font_size = int(image.size[0] / 50) + 5

        # First, draw all the rectangles with a transparent fill.
        # This is done on a temporary image with full opacity to then
        # make the whole layer semi-transparent, without needing to
        # carefully fill the gaps between boxes.
        FILL_ALPHA = 48
        fill_layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(fill_layer)
        for box in self.reference_boxes:
            draw.rectangle(box.as_tuple, fill="blue")
        for box in self.merged_extended_boxes:
            draw.rectangle(box.as_tuple, fill=(128, 0, 200, 255))  # Better purple
        for box in self.extended_boxes:
            draw.rectangle(box.as_tuple, fill="red")
        for box in self.boxes:
            draw.rectangle(box.as_tuple, fill="green")

        # Apply transparency to the fill layer
        alpha = fill_layer.split()[3]
        alpha = ImageEnhance.Brightness(alpha).enhance(FILL_ALPHA / 255)
        fill_layer.putalpha(alpha)

        # Composite the fill layer onto the original image
        image = Image.alpha_composite(image.convert("RGBA"), fill_layer)
        draw = ImageDraw.Draw(image)

        for index, box in enumerate(self.boxes):
            draw.rectangle(box.as_tuple, outline="green")
            # Draw the box number, with a white background, respecting font size.
            draw.text(
                (box.x1 + 4, box.y1),
                str(index + 1),
                fill="green",
                font=ImageFont.truetype(font_path, font_size),
                stroke_fill="white",
                stroke_width=3,
            )

        for box in self.extended_boxes:
            draw.rectangle(box.as_tuple, outline="red")
        for box in self.merged_extended_boxes:
            draw.rectangle(box.as_tuple, outline="purple")
        for box in self.reference_boxes:
            draw.rectangle(box.as_tuple, outline="blue")

        image.save(output_path)

    def make_box_mask(self, image_size: tuple[int, int], box_type: BoxType) -> Image:
        """
        Draw the boxes as a bitmap mask, where 1 represents box, and 0 represents no box.
        This is in essence just an image where the color is either black or white.

        :param image_size: The size of the image to make the mask for.
        :param box_type: The type of box to use.
        :return: The mask. Image mode: "1"
        """
        box_mask = Image.new("1", image_size, (0,))
        draw = ImageDraw.Draw(box_mask)
        boxes = self.boxes_from_type(box_type)
        for box in boxes:
            draw.rectangle(box.as_tuple, fill=(1,))
        return box_mask

    def resolve_total_overlaps(self) -> None:
        """
        Check the initial boxes for overlaps where the center of either box is within the other box.
        This kind of overlap means that the same text is included in both boxes, they aren't merely touching
        at the edges. This way we don't get duplicate text in OCR output.
        """
        # Place the extended boxes in the merged extended boxes, merging overlapping boxes.
        merge_queue = self.boxes.copy()
        merged_boxes = []
        while merge_queue:
            box = merge_queue.pop(0)
            # Find all boxes that overlap with this box.
            overlapping_boxes = [b for b in merge_queue if box.overlaps_center(b)]
            # Merge all overlapping boxes.
            for b in overlapping_boxes:
                box = box.merge(b)
                merge_queue.remove(b)
            merged_boxes.append(box)

        self.boxes = merged_boxes

    def resolve_overlaps(self, from_type: BoxType, to_type: BoxType, threshold: float) -> None:
        """
        Copy the extended boxes to the merged extended boxes, and merge overlapping boxes.

        :param from_type: The type of boxes to merge.
        :param to_type: The type of boxes to overwrite with the merged boxes.
        :param threshold: The threshold for the overlap check. Merge if more than this percentage (0-100) of the smaller
            box is covered by the larger box.
        """
        # Place the extended boxes in the merged extended boxes, merging overlapping boxes.
        merge_queue = set(self.boxes_from_type(from_type))
        merged_boxes = []
        while merge_queue:
            box = merge_queue.pop()
            # Find all boxes that overlap with this box.
            overlapping_boxes = [b for b in merge_queue if box.overlaps(b, threshold)]
            # Merge all overlapping boxes.
            for b in overlapping_boxes:
                box = box.merge(b)
                merge_queue.remove(b)
            merged_boxes.append(box)

        boxes_reference = self.boxes_from_type(to_type)
        boxes_reference.clear()
        boxes_reference.extend(merged_boxes)


@frozen
class OCRAnalytic:
    """
    Analytics data to quantify the OCR performance and bundle output in a usable format.
    - number of boxes
    - sizes of all boxes that were ocred
    - sizes of the boxes that were removed
    - the cached file name and the text and the box that was removed.
    """

    num_boxes: int
    box_sizes_ocr: Sequence[int]
    box_sizes_removed: Sequence[int]
    removed_box_data: Sequence[tuple[Path, str, Box]]


class OCRStatus(Enum):
    Normal = auto()
    Removed = auto()
    Edited = auto()
    EditedRemoved = auto()
    New = auto()


@define
class OCRResult:
    """
    A mutable variant that contains the OCR results for a single image.
    This is used in the OCR review window to allow human editing.

    The label contains the original index for process-created boxes,
    but newly created boxes are labeled with "New X", where X is the index of newly added boxes.

    bubbles: list[tuple[Path, str, Box, str, OCRStatus]]
    """

    path: Path
    text: str
    box: Box
    label: str
    status: OCRStatus


def convert_ocr_analytics_to_results(ocr_analytics: list[OCRAnalytic]) -> list[list[OCRResult]]:
    """
    We pretty much just extract the removed box data from the analytics, but attach a
    status to each box.

    :param ocr_analytics: The OCR analytics to convert.
    :return: The OCR results per image file.
    """
    ocr_results = []
    for analytic in ocr_analytics:
        bubbles = [
            OCRResult(path, text, box, str(index), OCRStatus.Normal)
            for index, (path, text, box) in enumerate(analytic.removed_box_data, start=1)
        ]
        ocr_results.append(bubbles)
    return ocr_results


def convert_ocr_results_to_analytics(ocr_results: list[list[OCRResult]]) -> list[OCRAnalytic]:
    """
    Just drop the status and leave the rest of the data blank,
    meaning only the removed box data is preserved.
    Boxes with the status Removed are dropped.

    :param ocr_results: The OCR results to convert.
    :return: The OCR analytics per image file.
    """
    ocr_analytics = []
    for results in ocr_results:
        removed_box_data = [
            (result.path, result.text, result.box)
            for result in results
            if result.status != OCRStatus.Removed
        ]
        ocr_analytics.append(
            OCRAnalytic(
                len(removed_box_data),
                [],
                [],
                removed_box_data,
            )
        )
    return ocr_analytics


@frozen
class MaskFittingAnalytic:
    """
    Analytics data to visualize the mask fitting performance.
    - The image path.
    - Whether a good enough fit was found.
    - The mask index chosen by the mask fitting process.
    - The standard deviation of the mask chosen by the mask fitting process.
    - The thickness of the mask chosen by the mask fitting process. None if the box mask.
    """

    image_path: Path
    fit_was_found: bool
    mask_index: int
    mask_std_deviation: float
    mask_thickness: int | None


@frozen
class MaskFittingResults:
    """
    This is a simple struct to hold the results from the mask fitting process.
    Since it returns a lot of data, this is more readable.
    """

    best_mask: Image
    median_color: int
    mask_coords: tuple[int, int]
    analytics_page_path: Path
    analytics_std_deviation: float
    analytics_mask_index: int
    analytics_thickness: int | None
    mask_box: Box  # Used for the denoising process.
    debug_masks: list[Image]

    @property
    def analytics(self) -> MaskFittingAnalytic:
        return MaskFittingAnalytic(
            self.analytics_page_path,
            not self.failed,
            self.analytics_mask_index,
            self.analytics_std_deviation,
            self.analytics_thickness,
        )

    @property
    def failed(self) -> bool:
        return self.best_mask is None

    @property
    def mask_data(self) -> tuple[Image, int, tuple[int, int]]:
        return self.best_mask, self.median_color, self.mask_coords

    @property
    def noise_mask_data(self) -> tuple[Box, float]:
        return self.mask_box, self.analytics_std_deviation


@frozen
class MaskerData:
    """
    This is a simple struct to hold the inputs for the masker.
    The data is a tuple of:
    - The json file path.
    - The image cache directory.
    - The masker config.
    - The extract text flag.
    - The show masks flag. (when true, save intermediate masks to the cache directory)
    - The debug flag.
    """

    json_path: Path
    cache_dir: Path
    masker_config: cfg.MaskerConfig
    extract_text: bool
    show_masks: bool
    debug: bool


@frozen
class MaskData:
    """
    This is a simple struct to hold all the extra information needed to perform the
    denoising process.

    - The original image path.
    - The cached image path.
    - The mask image path.
    - The scale of the original image to the base image.
    - The box coordinates with their respective standard deviation for the masks,
      whether they failed, and the mask thickness.
    """

    original_path: Path
    base_image_path: Path
    mask_path: Path
    scale: float
    boxes_with_stats: Sequence[tuple[Box, float, bool, int | None]]

    @classmethod
    def from_json(cls, json_str: str) -> "MaskData":
        """
        Create a MaskData object from a json string.
        Boxes with deviation need to be deserialized from tuples.

        :param json_str: The json string.
        :return: The MaskData object.
        """
        data = json.loads(json_str)
        return cls(
            Path(data["original_path"]),
            Path(data["base_image_path"]),
            Path(data["mask_path"]),
            data["scale"],
            [
                (Box(*box), deviation, failed, thickness)
                for box, deviation, failed, thickness in data["boxes_with_stats"]
            ],
        )

    def to_json(self) -> str:
        """
        Dump the MaskData object to a json string.
        Boxes are serialized as tuples.
        """
        # Convert the Path objects to strings.
        data = {
            "original_path": str(self.original_path),
            "base_image_path": str(self.base_image_path),
            "mask_path": str(self.mask_path),
            "scale": self.scale,
            "boxes_with_stats": [
                (box.as_tuple, deviation, failed, thickness)
                for box, deviation, failed, thickness in self.boxes_with_stats
            ],
        }
        return json.dumps(data, indent=4)


@frozen
class DenoiserData:
    """
    This is a simple struct to hold the inputs for the denoiser.
    The data is a tuple of:
    - The json file path.
    - The image cache directory.
    - The denoiser config.
    - The debug flag.
    """

    json_path: Path
    cache_dir: Path
    denoiser_config: cfg.DenoiserConfig
    debug: bool


@frozen
class DenoiseAnalytic:
    """
    Analytics data to visualize the denoising performance.
    - The standard deviations of the mask selection process. They are shown here,
      due to them being relevant to the min-threshold for choosing masks to denoise.
    - The path to the original image. This is needed to trace the analytics back to the image in the gui.
    """

    std_deviations: Sequence[float]
    path: Path


@frozen
class InpainterData:
    """
    This is a simple struct to hold the inputs for the inpainter.
    The data is a tuple of:
    - The page data json path. (This is #clean.json)
    - The mask data json path.  (This is #mask_data.json)
    - The image output directory.
    - The image cache directory.
    - The general config.
    - The masker config.  (For the min mask size)
    - The inpainter config.
    - The show masks flag. (when true, save intermediate masks to the cache directory)
    - The debug flag.
    """

    page_data_json_path: Path
    mask_data_json_path: Path
    cache_dir: Path
    masker_config: cfg.MaskerConfig
    denoiser_config: cfg.DenoiserConfig
    inpainter_config: cfg.InpainterConfig
    debug: bool


@frozen
class InpaintingAnalytic:
    """
    Analytics data to visualize the inpainting performance.
    - The thickness of the outline when inpainting.
    - The path to the original image.
    """

    thicknesses: Sequence[int]
    path: Path
