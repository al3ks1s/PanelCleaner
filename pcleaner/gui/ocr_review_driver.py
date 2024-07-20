import math
from enum import IntEnum
from functools import wraps

import PySide6.QtCore as Qc
import PySide6.QtGui as Qg
import PySide6.QtWidgets as Qw
from PySide6.QtCore import Slot
from loguru import logger

import pcleaner.gui.gui_utils as gu
import pcleaner.gui.image_file as imf
import pcleaner.gui.structures as gst
import pcleaner.structures as st
import pcleaner.ocr.ocr as ocr
from pcleaner.gui.ui_generated_files.ui_OcrReview import Ui_OcrReview

# The maximum size, will be smaller on one side if the image is not square.
THUMBNAIL_SIZE = 180


def suppress_cell_update_handling(method):
    @wraps(method)
    def wrapper(self, *args, **kwargs):
        self.awaiting_user_input = False
        result = method(self, *args, **kwargs)
        self.awaiting_user_input = True
        return result

    return wrapper


class ViewMode(IntEnum):
    WithBoxes = 0
    Original = 1


BUBBLE_STATUS_COLORS = {
    st.OCRStatus.Normal: Qg.QColor(0, 128, 0, 255),
    st.OCRStatus.Removed: Qg.QColor(128, 0, 0, 255),
    st.OCRStatus.EditedRemoved: Qg.QColor(128, 0, 0, 255),
    st.OCRStatus.Edited: Qg.QColor(0, 0, 128, 255),
    st.OCRStatus.New: Qg.QColor(128, 128, 0, 255),
}


class OcrReviewWindow(Qw.QDialog, Ui_OcrReview):
    """
    A window to display the process results.
    """

    images: list[imf.ImageFile]
    # These are the in the raw analytic format.
    ocr_analytics: list[st.OCRAnalytic]
    # These use the mutable format.
    ocr_results: list[list[st.OCRResult]]

    min_thumbnail_size: int
    max_thumbnail_size: int

    first_load: bool

    ocr_model: gst.Shared[ocr.OcrProcsType]
    theme_is_dark: gst.Shared[bool]

    # When True, handle changes to cells as user input.
    awaiting_user_input: bool

    def __init__(
        self,
        parent=None,
        images: list[imf.ImageFile] = None,
        ocr_analytics: list[st.OCRAnalytic] = None,
        ocr_model: gst.Shared[ocr.OcrProcsType] = None,
        theme_is_dark: gst.Shared[bool] = None,
    ):
        """
        Init the widget.

        :param ocr_model:
        :param parent: The parent widget.
        :param images: The images to display.
        :param ocr_analytics: The OCR results to display.
        :param ocr_model: The OCR model to use.
        :param theme_is_dark: The shared theme state.
        """
        logger.debug(f"Opening OCR Review Window for {len(images)} outputs.")
        Qw.QDialog.__init__(self, parent)
        self.setupUi(self)

        # Special OutputReview overrides.
        self.target_output = imf.Output.initial_boxes
        self.confirm_closing = True

        self.images = images
        self.ocr_analytics = ocr_analytics
        self.theme_is_dark = theme_is_dark

        self.awaiting_user_input = False

        if len(self.images) != len(self.ocr_analytics):
            logger.error("The number of images and OCR results don't match.")

        self.ocr_results = st.convert_ocr_analytics_to_results(ocr_analytics)

        self.first_load = True

        # Set the table to only allow editing in the text column.
        self.tableWidget_ocr.setEditableColumns([1])

        self.calculate_thumbnail_size()
        self.init_arrow_buttons()
        self.init_image_list()
        self.image_list.currentItemChanged.connect(self.handle_image_change)
        # Select the first image to start with.
        self.comboBox_view_mode.currentIndexChanged.connect(self.update_image_boxes)
        self.tableWidget_ocr.currentRowChanged.connect(self.update_image_boxes)
        self.image_list.setCurrentRow(0)

        # Let the user select the bubble by clicking it too.
        self.image_viewer.bubble_clicked.connect(self.handle_bubble_clicked)

        self.pushButton_done.clicked.connect(self.close)

        # Connect the zoom buttons to the image viewer.
        self.pushButton_zoom_in.clicked.connect(self.image_viewer.zoom_in)
        self.pushButton_zoom_out.clicked.connect(self.image_viewer.zoom_out)
        self.pushButton_zoom_fit.clicked.connect(self.image_viewer.zoom_fit)
        self.pushButton_zoom_reset.clicked.connect(self.image_viewer.zoom_reset)

        # Connect the table buttons.
        self.pushButton_new.toggled.connect(self.new_bubble)
        self.pushButton_delete.clicked.connect(self.delete_bubble)
        self.pushButton_reset.clicked.connect(self.reset_single_bubble)
        self.pushButton_reset_all.clicked.connect(self.reset_all_bubbles)
        self.pushButton_row_up.clicked.connect(self.move_row_up)
        self.pushButton_row_down.clicked.connect(self.move_row_down)
        self.pushButton_undelete.clicked.connect(self.delete_bubble)
        self.tableWidget_ocr.currentRowChanged.connect(self.update_button_availability)
        self.tableWidget_ocr.cellChanged.connect(self.handle_table_edited)

        # Callback for new bubble creation.
        self.image_viewer.new_bubble.connect(self.add_new_bubble)

        # Make side splitter 50/50.
        side_splitter_width = self.splitter_side.width()
        self.splitter_side.setSizes([side_splitter_width // 2, side_splitter_width // 2])

        self.load_custom_icons()
        self.update_button_availability()

        # Set the new button color to match what would be assigned to new buttons.
        self.image_viewer.set_new_bubble_color(BUBBLE_STATUS_COLORS[st.OCRStatus.New])

    def load_custom_icons(self) -> None:
        # Load the custom new_bubble icon.
        if self.theme_is_dark.get():
            icon_new = Qg.QIcon(":/custom_icons/dark/new_bubble.svg")
            icon_undelete = Qg.QIcon(":/custom_icons/dark/trash_restore.svg")
        else:
            icon_new = Qg.QIcon(":/custom_icons/light/new_bubble.svg")
            icon_undelete = Qg.QIcon(":/custom_icons/light/trash_restore.svg")

        # Check if it loaded correctly.
        if icon_new.isNull():
            logger.error("Failed to load icon new_bubble.")
            return
        if icon_undelete.isNull():
            logger.error("Failed to load icon trash_restore.")
            return

        self.pushButton_new.setIcon(icon_new)
        self.pushButton_new.setText("")
        self.pushButton_undelete.setIcon(icon_undelete)
        self.pushButton_undelete.setText("")

    def switch_to_image(self, image: imf.ImageFile) -> None:
        """
        Show the image in the button in the image view.

        :param image: The image to show.
        """
        original_path = image.path

        self.label_file_name.setText(str(original_path))
        self.label_file_name.setToolTip(str(original_path))
        self.label_file_name.setElideMode(Qc.Qt.ElideLeft)

        self.image_viewer.set_image(original_path)

        self.load_ocr_results(self.ocr_results[self.images.index(image)])

        # Set a delayed zoom to fit.
        if self.first_load:
            Qc.QTimer.singleShot(0, self.image_viewer.zoom_fit)
            self.first_load = False

    @suppress_cell_update_handling
    def load_ocr_results(self, ocr_results: list[st.OCRResult]) -> None:
        """
        Load the OCR results into the text fields.

        :param ocr_results: The OCR results to load for this image.
        """
        # Format OCR results.
        self.tableWidget_ocr.clearAll()
        # text = ""
        # for index, (_, ocr_text, _) in enumerate(ocr_result.removed_box_data):
        #     text += f"{index + 1}\t {ocr_text}\n\n"
        #
        # if not text:
        #     text = f'<i>{self.tr("No text found in the image.")}</i>'
        #
        # self.textEdit_ocr.setPlainText(text)

        # This index is for the table to keep track of the index into the specific OCR result list.
        # It isn't the original index of the box as shown in the image.
        for index, ocr_result in enumerate(ocr_results):
            self.tableWidget_ocr.appendRow(
                str(ocr_result.label),
                ocr_result.text,
            )
            # If the status is edited, make it italic, removed is strikeout, and
            # editedremoved is both.
            cell_font = self.tableWidget_ocr.item(index, 1).font()
            if ocr_result.status == st.OCRStatus.Edited:
                cell_font.setItalic(True)
            elif ocr_result.status == st.OCRStatus.Removed:
                cell_font.setStrikeOut(True)
            elif ocr_result.status == st.OCRStatus.EditedRemoved:
                cell_font.setItalic(True)
                cell_font.setStrikeOut(True)
            self.tableWidget_ocr.item(index, 1).setFont(cell_font)

        self.update_image_boxes()

    def update_image_boxes(self) -> None:
        """
        Update the image viewer with the current OCR results.
        """
        if self.view_mode() == ViewMode.Original or not self.ocr_results:
            self.image_viewer.clear_bubbles()
            return

        ocr_results = self.current_image_ocr_results()

        rects: list[Qc.QRect] = [
            Qc.QRect(*ocr_result.box.as_tuple_xywh) for ocr_result in ocr_results
        ]
        colors: list[Qg.QColor] = [
            BUBBLE_STATUS_COLORS[ocr_result.status] for ocr_result in ocr_results
        ]
        labels: list[str] = [str(ocr_result.label) for ocr_result in ocr_results]
        strokes: list[Qc.Qt.PenStyle] = list(
            map(
                lambda r: (
                    Qc.Qt.SolidLine
                    if r.status not in (st.OCRStatus.Removed, st.OCRStatus.EditedRemoved)
                    else Qc.Qt.DashLine
                ),
                ocr_results,
            )
        )
        # assert self.tableWidget_ocr.rowCount() == len(ocr_results)
        # assert self.tableWidget_ocr.rowCount() == len(rects)
        # assert self.tableWidget_ocr.rowCount() == len(colors)
        # assert self.tableWidget_ocr.rowCount() == len(labels)
        # assert self.tableWidget_ocr.rowCount() == len(strokes)

        # Set the selected bubble to have the highlight color.
        selected_row = self.tableWidget_ocr.currentRow()
        if selected_row != -1:
            colors[selected_row] = self.palette().highlight().color()
        self.image_viewer.set_bubbles(rects, colors, labels, strokes)

    def view_mode(self) -> ViewMode:
        """
        Get the current view mode.

        :return: The current view mode.
        """
        return ViewMode(self.comboBox_view_mode.currentIndex())

    def current_image_ocr_results(self) -> list[st.OCRResult]:
        """
        Get the OCR results for the currently selected image.

        :return: The OCR results.
        """
        return self.ocr_results[self.image_list.currentRow()]

    @Slot(int)
    def handle_bubble_clicked(self, index: int) -> None:
        self.tableWidget_ocr.setCurrentCell(index, 1)

    # Table manipulation functions =================================================================

    def update_button_availability(self) -> None:
        """
        Check which buttons should be enabled.
        """
        selected_row = self.tableWidget_ocr.currentRow()
        self.pushButton_row_up.setEnabled(selected_row > 0)
        self.pushButton_row_down.setEnabled(
            selected_row != -1 and selected_row < self.tableWidget_ocr.rowCount() - 1
        )
        # For resetting a single bubble, both the index needs to
        # be valid and the bubble must be edited.
        if selected_row == -1:
            self.pushButton_reset.setEnabled(False)
        else:
            ocr_results = self.current_image_ocr_results()
            self.pushButton_reset.setEnabled(
                ocr_results[selected_row].status
                in (st.OCRStatus.Edited, st.OCRStatus.EditedRemoved)
            )
        # For delete there must be a selection and it can't be deleted already.
        self.pushButton_undelete.hide()
        if selected_row == -1:
            self.pushButton_delete.setEnabled(False)
            self.pushButton_delete.show()
            self.pushButton_undelete.hide()
        else:
            ocr_results = self.current_image_ocr_results()
            self.pushButton_delete.setEnabled(True)
            if ocr_results[selected_row].status in (
                st.OCRStatus.Removed,
                st.OCRStatus.EditedRemoved,
            ):
                self.pushButton_delete.hide()
                self.pushButton_undelete.show()
            else:
                self.pushButton_delete.show()
                self.pushButton_undelete.hide()

    @suppress_cell_update_handling
    def delete_bubble(self) -> None:
        """
        Remove the currently selected bubble.
        If it was a new bubble, remove it entirely.
        Otherwise, mark it as removed.
        But if already removed, undelete it.
        """
        selected_row = self.tableWidget_ocr.currentRow()
        if selected_row == -1:
            return

        ocr_results = self.current_image_ocr_results()
        ocr_result = ocr_results[selected_row]

        if ocr_result.status == st.OCRStatus.New:
            ocr_results.pop(selected_row)
        elif ocr_result.status == st.OCRStatus.Removed:
            ocr_result.status = st.OCRStatus.Normal
        elif ocr_result.status == st.OCRStatus.EditedRemoved:
            ocr_result.status = st.OCRStatus.Edited
        elif ocr_result.status == st.OCRStatus.Edited:
            ocr_result.status = st.OCRStatus.EditedRemoved
        else:
            ocr_result.status = st.OCRStatus.Removed

        self.load_ocr_results(ocr_results)

    @suppress_cell_update_handling
    def reset_single_bubble(self) -> None:
        """
        Reset only the currently selected bubble.
        Go by the original label to find the correct bubble.
        """
        selected_row = self.tableWidget_ocr.currentRow()
        if selected_row == -1:
            return
        # Check that this is an edited bubble.
        ocr_results = self.current_image_ocr_results()
        if ocr_results[selected_row].status not in (
            st.OCRStatus.Edited,
            st.OCRStatus.EditedRemoved,
        ):
            logger.error("Tried to reset a non-edited bubble.")
            return

        image_index = self.image_list.currentRow()
        original_analytic = self.ocr_analytics[image_index]
        # Wrap it in a list due to the way the helper is written, then unpack the result.
        original_results = st.convert_ocr_analytics_to_results([original_analytic])[0]

        ocr_results = self.ocr_results[image_index]
        # Figure out what the original row was by matching the label.
        original_row = next(
            i
            for i, ocr_result in enumerate(original_results)
            if ocr_result.label == ocr_results[selected_row].label
        )
        ocr_results[selected_row] = original_results[original_row]

        self.load_ocr_results(ocr_results)

    @suppress_cell_update_handling
    def reset_all_bubbles(self) -> None:
        """
        Reset all bubbles to their original state.
        """
        # Confirm first.
        if (
            gu.show_question(
                self,
                self.tr("Reset Bubbles"),
                self.tr("Are you sure you want to reset all boxes for this image?"),
            )
            == Qw.QMessageBox.Cancel
        ):
            return
        # Pull the original OCR results from the analytics.
        image_index = self.image_list.currentRow()
        original_analytic = self.ocr_analytics[image_index]
        # Wrap it in a list due to the way the helper is written, then unpack the result.
        original_results = st.convert_ocr_analytics_to_results([original_analytic])[0]

        self.ocr_results[image_index] = original_results
        self.load_ocr_results(original_results)

    @suppress_cell_update_handling
    def move_row_up(self) -> None:
        """
        Move the currently selected row up.
        """
        selected_row = self.tableWidget_ocr.currentRow()
        if selected_row == -1 or selected_row == 0:
            return

        ocr_results = self.current_image_ocr_results()
        ocr_results.insert(selected_row - 1, ocr_results.pop(selected_row))

        self.load_ocr_results(ocr_results)
        self.tableWidget_ocr.setCurrentCell(selected_row - 1, 1)

    @suppress_cell_update_handling
    def move_row_down(self) -> None:
        """
        Move the currently selected row down.
        """
        selected_row = self.tableWidget_ocr.currentRow()
        if selected_row == -1 or selected_row == self.tableWidget_ocr.rowCount() - 1:
            return

        ocr_results = self.current_image_ocr_results()
        ocr_results.insert(selected_row + 1, ocr_results.pop(selected_row))

        self.load_ocr_results(ocr_results)
        self.tableWidget_ocr.setCurrentCell(selected_row + 1, 1)

    def new_bubble(self) -> None:
        """
        Switch the image viewer to bubble mode.
        The actual bubble creation is done once the callback comes in on
        BubbleImageViewer.new_bubble.
        """
        self.image_viewer.set_allow_drawing_bubble(self.pushButton_new.isChecked())

    @suppress_cell_update_handling
    def add_new_bubble(self, rect: Qc.QRect) -> None:
        # Disable new bubble mode.
        self.pushButton_new.setChecked(False)
        self.new_bubble()

        # Create regular xyxy coordinates for a box.
        x1, y1 = rect.topLeft().x(), rect.topLeft().y()
        x2, y2 = rect.bottomRight().x(), rect.bottomRight().y()
        box = st.Box(x1, y1, x2, y2)

        # Discard if the bubble is too small.
        MIN_BUBBLE_SIZE = 400
        if box.area < MIN_BUBBLE_SIZE:
            logger.debug("Discarding bubble due to size.")
            return

        # Check how many new bubbles there already are to label this one correctly.
        new_bubbles = sum(
            ocr_result.status == st.OCRStatus.New for ocr_result in self.current_image_ocr_results()
        )
        new_bubble_label = self.tr("New") + f" {new_bubbles + 1}"

        # Add the new bubble to the current image.
        ocr_results = self.current_image_ocr_results()
        image_path = ocr_results[0].path
        ocr_results.append(st.OCRResult(image_path, "", box, new_bubble_label, st.OCRStatus.New))

        self.load_ocr_results(ocr_results)

    @Slot(int, int)
    def handle_table_edited(self, row: int, col: int) -> None:
        """
        Store the edited text in the OCR results.

        :param row: The row of the edited cell.
        :param col: The column of the edited cell, must be 1, the label may not be changed.
        """
        if not self.awaiting_user_input:
            return

        if col != 1:
            logger.error("Only the text column should be editable.")
            return

        ocr_results = self.current_image_ocr_results()
        ocr_results[row].text = self.tableWidget_ocr.currentText(1)
        # If the cell was normal, mark it as edited.
        if ocr_results[row].status == st.OCRStatus.Normal:
            ocr_results[row].status = st.OCRStatus.Edited
        if ocr_results[row].status == st.OCRStatus.Removed:
            ocr_results[row].status = st.OCRStatus.EditedRemoved

        self.load_ocr_results(ocr_results)

    # Copied shit from OutputReview ================================================================

    def closeEvent(self, event: Qg.QCloseEvent) -> None:
        if self.confirm_closing:
            if (
                gu.show_question(
                    self,
                    self.tr("Finish Review"),
                    self.tr("Are you sure you want to finish the review?"),
                )
                == Qw.QMessageBox.Cancel
            ):
                event.ignore()
                return
        event.accept()

    def calculate_thumbnail_size(self) -> None:
        """
        Use the current monitor's resolution to calculate the thumbnail size.
        The min value is 1% of the screen width, the max value is 100%.
        """
        screen = Qw.QApplication.primaryScreen()
        screen_size = screen.size()
        screen_width = screen_size.width()
        self.min_thumbnail_size = int(screen_width * 0.01)
        self.max_thumbnail_size = screen_width

    def to_log_scale(self, value: int) -> int:
        """
        Convert a linear value to a logarithmic scale.
        """
        min_log = math.log(self.min_thumbnail_size)
        max_log = math.log(self.max_thumbnail_size)
        scale = (max_log - min_log) / (
            self.horizontalSlider_icon_size.maximum() - self.horizontalSlider_icon_size.minimum()
        )
        log_value = min_log + (value - self.horizontalSlider_icon_size.minimum()) * scale
        return int(math.exp(log_value))

    def from_log_scale(self, value: int) -> int:
        """
        Convert a logarithmic value to a linear scale.
        """
        min_log = math.log(self.min_thumbnail_size)
        max_log = math.log(self.max_thumbnail_size)
        scale = (max_log - min_log) / (
            self.horizontalSlider_icon_size.maximum() - self.horizontalSlider_icon_size.minimum()
        )
        linear_value = (
            math.log(value) - min_log
        ) / scale + self.horizontalSlider_icon_size.minimum()
        return int(linear_value)

    def init_image_list(self) -> None:
        """
        Populate the image list with the images.
        """
        # Reduce the size of the list. Make it a 1/3 split.
        window_width = self.width()
        self.splitter.setSizes([window_width // 3, 2 * window_width // 3])

        self.image_list.setIconSize(Qc.QSize(THUMBNAIL_SIZE, THUMBNAIL_SIZE))
        # Use a logarithmic scale to distribute the values.
        self.horizontalSlider_icon_size.setValue(self.from_log_scale(THUMBNAIL_SIZE))
        self.horizontalSlider_icon_size.valueChanged.connect(self.update_icon_size)

        self.label_image_count.setText(self.tr("%n image(s)", "", len(self.images)))

        for image in self.images:

            original_path = image.path
            output_path = image.outputs[self.target_output].path

            if output_path is None:
                logger.error(f"Output path for {image.path} is None.")

            try:
                self.image_list.addItem(
                    Qw.QListWidgetItem(Qg.QIcon(str(output_path)), str(original_path.stem))
                )
            except OSError:
                gu.show_exception(
                    self,
                    self.tr("Loading Error"),
                    self.tr("Failed to load image '{path}'").format(path=output_path),
                )

        # Sanity check.
        if len(self.images) != self.image_list.count():
            logger.error("Failed to populate the image list correctly.")

    def init_arrow_buttons(self) -> None:
        """
        Connect the arrow buttons to the image list.
        """
        self.pushButton_next.clicked.connect(
            lambda: self.image_list.setCurrentRow(self.image_list.currentRow() + 1)
        )
        self.pushButton_prev.clicked.connect(
            lambda: self.image_list.setCurrentRow(self.image_list.currentRow() - 1)
        )

        # Disable the buttons if there is no next or previous image.
        self.image_list.currentRowChanged.connect(self.check_arrow_buttons)

    def check_arrow_buttons(self) -> None:
        """
        Check if the arrow buttons should be enabled or disabled.
        """
        current_row = self.image_list.currentRow()
        self.pushButton_prev.setEnabled(current_row > 0)
        self.pushButton_next.setEnabled(current_row < self.image_list.count() - 1)

    @Slot(int)
    def update_icon_size(self, size: int) -> None:
        """
        Update the icon size in the image list.

        :param size: The new size.
        """
        size = self.to_log_scale(size)
        self.image_list.setIconSize(Qc.QSize(size, size))

    @Slot(Qw.QListWidgetItem, Qw.QListWidgetItem)
    @Slot(Qw.QListWidgetItem)
    def handle_image_change(
        self, current: Qw.QListWidgetItem = None, previous: Qw.QListWidgetItem = None
    ) -> None:
        """
        Figure out what the new image is and display it.
        """
        # We don't need the previous image, so we ignore it.
        index = self.image_list.currentRow()
        image = self.images[index]
        self.switch_to_image(image)
