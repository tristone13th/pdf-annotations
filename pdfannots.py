import sys, io, argparse, os, datetime
from pdfminer.pdfinterp import PDFResourceManager, PDFPageInterpreter
from pdfminer.pdfpage import PDFPage
from pdfminer.layout import LAParams, LTContainer, LTAnno, LTChar, LTTextBox
from pdfminer.converter import TextConverter
from pdfminer.pdfparser import PDFParser
from pdfminer.pdfdocument import PDFDocument, PDFNoOutlines
from pdfminer.psparser import PSLiteralTable, PSLiteral
import pdfminer.pdftypes as pdftypes
import pdfminer.settings
import pdfminer.utils

from typing import List
from pathlib import Path

pdfminer.settings.STRICT = False

SUBSTITUTIONS = {
    u'ﬀ': 'ff',
    u'ﬁ': 'fi',
    u'ﬂ': 'fl',
    u'ﬃ': 'ffi',
    u'ﬄ': 'ffl',
    u'‘': "'",
    u'’': "'",
    u'“': '"',
    u'”': '"',
    u'…': '...',
}

ANNOTATION_SUBTYPES = frozenset({'Text', 'Highlight', 'Squiggly', 'StrikeOut', 'Underline'})
ANNOTATION_NITS = frozenset({'Squiggly', 'StrikeOut', 'Underline'})

COLUMNS_PER_PAGE = 2  # default only, changed via a command-line parameter


def box_hit(item, box):
    (x0, y0, x1, y1) = box
    assert item.x0 <= item.x1 and item.y0 <= item.y1
    assert x0 <= x1 and y0 <= y1

    # does most of the item area overlap the box?
    # http://math.stackexchange.com/questions/99565/simplest-way-to-calculate-the-intersect-area-of-two-rectangles
    x_overlap = max(0, min(item.x1, x1) - max(item.x0, x0))
    y_overlap = max(0, min(item.y1, y1) - max(item.y0, y0))
    overlap_area = x_overlap * y_overlap
    item_area = (item.x1 - item.x0) * (item.y1 - item.y0)
    assert overlap_area <= item_area

    if item_area == 0:
        return False
    else:
        return overlap_area >= 0.5 * item_area


class RectExtractor(TextConverter):
    def __init__(self, resource_manager, codec='utf-8', page_number=1, la_params=None):
        dummy = io.StringIO()
        TextConverter.__init__(self, resource_manager, outfp=dummy, codec=codec, pageno=page_number, laparams=la_params)
        self.annotations = set()
        self._last_hit = frozenset()
        self._cur_line = set()

    def set_annotations(self, annotations):
        self.annotations = {a for a in annotations if a.boxes}

    # main callback from parent PDFConverter
    def receive_layout(self, lt_page):
        self.render(lt_page)
        self._last_hit = frozenset()
        self._cur_line = set()

    def test_boxes(self, item):
        hits = frozenset({a for a in self.annotations if any({box_hit(item, b) for b in a.boxes})})
        self._last_hit = hits
        self._cur_line.update(hits)
        return hits

    # "broadcast" newlines to _all_ annotations that received any text on the
    # current line, in case they see more text on the next line, even if the
    # most recent character was not covered.
    def capture_newline(self):
        for a in self._cur_line:
            a.capture('\n')
        self._cur_line = set()

    def render(self, item):
        # If it's a container, recurse on nested items.
        if isinstance(item, LTContainer):
            for child in item:
                self.render(child)

            # Text boxes are a subclass of container, and somehow encode newlines
            # (this weird logic is derived from pdfminer.converter.TextConverter)
            if isinstance(item, LTTextBox):
                self.test_boxes(item)
                self.capture_newline()

        # Each character is represented by one LTChar, and we must handle
        # individual characters (not higher-level objects like LTTextLine)
        # so that we can capture only those covered by the annotation boxes.
        elif isinstance(item, LTChar):
            for a in self.test_boxes(item):
                a.capture(item.get_text())

        # Annotations capture whitespace not explicitly encoded in
        # the text. They don't have an (X,Y) position, so we need some
        # heuristics to match them to the nearby annotations.
        elif isinstance(item, LTAnno):
            text = item.get_text()
            if text == '\n':
                self.capture_newline()
            else:
                for a in self._last_hit:
                    a.capture(text)


class Page:
    def __init__(self, page_number, media_box):
        self.page_number = page_number
        self.media_box = media_box
        self.annotations = []

    def __eq__(self, other):
        return self.page_number == other.page_number

    def __lt__(self, other):
        return self.page_number < other.page_number


class Pos:
    def __init__(self, page, x, y):
        self.page = page
        self.x = x if x else 0
        self.y = y if y else 0

    def __lt__(self, other):
        """
        how to compare two positions? first by page, then by column (multi columns), finally by y
        :param other: other position
        :return: Boolean (less than or not)
        """
        if self.page < other.page:
            return True
        elif self.page == other.page:
            assert self.page is other.page
            (sx, sy) = self.normalise_to_media_box()
            (ox, oy) = other.normalise_to_media_box()
            (x0, y0, x1, y1) = self.page.media_box
            colwidth = (x1 - x0) / COLUMNS_PER_PAGE
            self_col = (sx - x0) // colwidth
            other_col = (ox - x0) // colwidth
            return self_col < other_col or (self_col == other_col and sy > oy)
        else:
            return False

    def normalise_to_media_box(self):
        """
        normalise position to prevent over-floating
        :return: x and y
        """
        x, y = self.x, self.y
        (x0, y0, x1, y1) = self.page.media_box
        if x < x0:
            x = x0
        elif x > x1:
            x = x1
        if y < y0:
            y = y0
        elif y > y1:
            y = y1
        return x, y


class Annotation:
    def __init__(self, page, tag_name, coords=None, rect=None, contents=None, author=None):
        self.page = page
        self.tag_name = tag_name
        self.contents = None if contents == '' else contents
        self.rect = rect
        self.author = author
        self.text = ''

        if coords is None:
            self.boxes = None
        else:
            assert len(coords) % 8 == 0
            self.boxes = []
            while coords:
                (x0, y0, x1, y1, x2, y2, x3, y3), coords = coords[:8], coords[8:]
                x_values = (x0, x1, x2, x3)
                y_values = (y0, y1, y2, y3)
                box = (min(x_values), min(y_values), max(x_values), max(y_values))
                self.boxes.append(box)

    def capture(self, text):
        if text == '\n':
            # Kludge for latex: elide hyphens
            if self.text.endswith('-'):
                self.text = self.text[:-1]

            # Join lines, treating newlines as space, while ignoring successive
            # newlines. This makes it easier for the renderer to
            # "broadcast" LTAnno newlines to active annotations regardless of
            # box hits. (Detecting paragraph breaks is tricky anyway, and left
            # for future future work!)
            elif not self.text.endswith(' '):
                self.text += ' '
        else:
            self.text += text

    def get_text(self):
        if self.boxes:
            if self.text:
                # replace tex ligatures (and other common odd characters)
                return ''.join([SUBSTITUTIONS.get(c, c) for c in self.text.strip()])
            else:
                # something's strange -- we have boxes but no text for them
                return "(XXX: missing text!)"
        else:
            return None

    def get_start_pos(self):
        if self.rect:
            (x0, y0, x1, y1) = self.rect
        elif self.boxes:
            (x0, y0, x1, y1) = self.boxes[0]
        else:
            return None
        return Pos(self.page, min(x0, x1), max(y0, y1))

    def __lt__(self, other):
        if isinstance(other, Annotation):
            return self.get_start_pos() < other.get_start_pos()
        return self.get_start_pos() < other.pos


class Outline:
    def __init__(self, level, title: str, dest, pos: Pos):
        self.level = level

        # remove level information in title
        assert title
        self.title = title.strip()
        self.dest = dest
        self.pos = pos

    def __lt__(self, other):
        if isinstance(other, Annotation):
            return self.pos < other.get_start_pos()
        return self.pos < other.pos


class NoInputFileError(Exception):
    pass


def format_annotation(annotation, extra=None):
    rawtext = annotation.get_text()
    comment = [l for l in annotation.contents.splitlines() if l] if annotation.contents else []
    text = [l for l in rawtext.strip().splitlines() if l] if rawtext else []

    # we are either printing: item text and item contents, or one of the two
    # if we see an annotation with neither, something has gone wrong
    assert text or comment

    # compute the formatted position (and extra bit if needed) as a label
    label = "Page %d (%s)." % (annotation.page.page_number + 1, extra if extra else "")

    ret = " * "
    if comment:
        ret += '\n'.join(comment)
    if text:
        ret += '\n'
        for index, para in enumerate(text):
            ret += "   > " + para
            if index == len(text) - 1:
                ret += " | " + label
            ret += "\n"
    else:
        ret += " | " + label + "\n"
    return ret


class PrettyPrinter:
    """
    Pretty-print the extracted annotations according to the output options.
    """

    def __init__(self, stem: str):
        self.stem = stem

    def print_all(self, outlines: List[Outline], annotations: List[Annotation], outfile):
        # print yaml header
        print('---', file=outfile)
        print('categories: Reading Notes', file=outfile)
        print('title: Reading Notes for ' + self.stem, file=outfile)
        print('---\n', file=outfile)

        # print outlines and annotations
        all_items = sorted(outlines + annotations)
        for a in all_items:
            if isinstance(a, Outline):
                print("#" * a.level + " " + a.title + "\n", file=outfile)
            else:
                print(format_annotation(a, a.tag_name), file=outfile)


def resolve_dest(doc, dest):
    if isinstance(dest, bytes):
        dest = pdftypes.resolve1(doc.get_dest(dest))
    elif isinstance(dest, PSLiteral):
        dest = pdftypes.resolve1(doc.get_dest(dest.name))
    if isinstance(dest, dict):
        dest = dest['D']
    return dest


def get_outlines(doc, page_list, page_dict) -> List[Outline]:
    result = []
    for (level, title, dest_name, action_ref, _) in doc.get_outlines():
        if dest_name is None and action_ref:
            action = pdftypes.resolve1(action_ref)
            if isinstance(action, dict):
                subtype = action.get('S')
                if subtype is PSLiteralTable.intern('GoTo'):
                    dest_name = action.get('D')
        if dest_name is None:
            continue
        dest = resolve_dest(doc, dest_name)

        # consider targets of the form [page /XYZ left top zoom]
        if dest[1] is PSLiteralTable.intern('XYZ'):
            (page_ref, _, target_x, target_y) = dest[:4]
        elif dest[1] is PSLiteralTable.intern('Fit'):
            (page_ref, _) = dest[:2]
            target_x, target_y = 0, float("inf")
        else:
            continue

        if type(page_ref) is int:
            page = page_list[page_ref]
        elif isinstance(page_ref, pdftypes.PDFObjRef):
            page = page_dict[page_ref.objid]
        else:
            sys.stderr.write('Warning: unsupported page reference in outline: %s\n' % page_ref)
            page = None

        if page:
            pos = Pos(page, target_x, target_y)
            result.append(Outline(level, title, dest_name, pos))
    return result


def get_annotations(pdf_annotations, page):
    annotations = []
    for pa in pdf_annotations:
        subtype = pa.get('Subtype')
        if subtype is not None and subtype.name not in ANNOTATION_SUBTYPES:
            continue

        contents = pa.get('Contents')
        if contents is not None:
            # decode as string, normalise line endings, replace special characters
            contents = pdfminer.utils.decode_text(contents)
            contents = contents.replace('\r\n', '\n').replace('\r', '\n')
            contents = ''.join([SUBSTITUTIONS.get(c, c) for c in contents])

        coords = pdftypes.resolve1(pa.get('QuadPoints'))
        rect = pdftypes.resolve1(pa.get('Rect'))
        author = pdftypes.resolve1(pa.get('T'))
        if author is not None:
            author = pdfminer.utils.decode_text(author)
        a = Annotation(page, subtype.name, coords, rect, contents, author=author)
        annotations.append(a)

    return annotations


def process_file(fh):
    resource_manager = PDFResourceManager()
    la_params = LAParams()
    device = RectExtractor(resource_manager, la_params=la_params)
    interpreter = PDFPageInterpreter(resource_manager, device)
    parser = PDFParser(fh)
    doc = PDFDocument(parser)

    page_list = []  # pages in page order
    page_dict = {}  # map from PDF page object ID to Page object
    all_annotations = []

    for (page_number, pdf_page) in enumerate(PDFPage.create_pages(doc)):
        print("Current processing page: ", page_number)
        page = Page(page_number, pdf_page.mediabox)
        page_list.append(page)
        page_dict[pdf_page.pageid] = page
        if pdf_page.annots:
            pdf_annotations = []
            for a in pdftypes.resolve1(pdf_page.annots):
                if isinstance(a, pdftypes.PDFObjRef):
                    pdf_annotations.append(a.resolve())
                else:
                    sys.stderr.write('Warning: unknown annotation: %s\n' % a)

            page.annotations = get_annotations(pdf_annotations, page)
            page.annotations.sort()
            device.set_annotations(page.annotations)
            interpreter.process_page(pdf_page)  # add text to annotation
            all_annotations.extend(page.annotations)

    outlines = []
    try:
        outlines = get_outlines(doc, page_list, page_dict)
    except PDFNoOutlines:
        sys.stderr.write("Document doesn't include outlines (\"bookmarks\")\n")
    except Exception as ex:
        sys.stderr.write("Warning: failed to retrieve outlines: %s\n" % ex)

    device.close()

    return outlines, all_annotations


def parse_args():
    p = argparse.ArgumentParser(description=__doc__)

    p.add_argument("input", metavar="INFILE", type=argparse.FileType("rb"), default=sys.stdin, nargs='?',
                   help="PDF file to process")
    p.add_argument("-o", metavar="OUTFILE", type=argparse.FileType("w", encoding="UTF-8"), dest="output",
                   default=sys.stdout, const=sys.stdout, nargs='?', help="output file (default is stdout)")
    p.add_argument("-n", "--cols", default=1, type=int, metavar="COLS", dest="cols",
                   help="number of columns per page in the document (default: 1)")

    return p.parse_args()


def main():
    args = parse_args()

    # reset input file
    if args.input is sys.stdin:
        f_list = os.listdir()
        for f in f_list:
            suffix = Path(f).suffix
            if suffix == '.pdf' or suffix == '.PDF':
                args.input = open(f, "rb")
                break
        if args.input is sys.stdin:
            raise NoInputFileError

    # reset output file
    stem = Path(args.input.name).stem.strip()
    args.output = open(str(datetime.date.today()) + '-' + stem + '.md', "w", encoding="UTF-8") if args.output is sys.stdout else args.output

    # reset columns
    global COLUMNS_PER_PAGE
    COLUMNS_PER_PAGE = args.cols

    # processing
    outlines, annotations = process_file(args.input)
    pp = PrettyPrinter(stem)
    pp.print_all(outlines, annotations, args.output)
    return 0


if __name__ == "__main__":
    try:
        main()
    except NoInputFileError:
        print("No PDF file under current directory.")
