from enum import Enum
import json
import os.path
from dataclasses import asdict, field
from dataclasses import dataclass
from pprint import pprint
from typing import Optional, Union
from functools import wraps

from google.auth.external_account_authorized_user import Credentials as ExternalCreds
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# If modifying these scopes, delete the file token.json.
SCOPES = ["https://www.googleapis.com/auth/drive.file"]

# A local copy of the spreadsheet
SPREADSHEET_FILENAME = "test_spreadsheet.json"


@dataclass
class Coordinate:
    sheet_id: int
    row_index: int
    column_index: int


def snake_case_to_camel_case(data: str) -> str:
    data_list: list[str] = data.split("_")
    for i in range(1, len(data_list)):
        data_list[i] = data_list[i][0].upper() + data_list[i][1:]
    return "".join(data_list)


def camel_case_dict_factory(data) -> dict:
    """Remap the keys from snake_case to camelCase."""
    return {snake_case_to_camel_case(field[0]): field[1] for field in data}


@dataclass
class Exercise:
    name: str
    url: str


def sheets_api_call(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        creds = get_creds()
        try:
            service = build("sheets", "v4", credentials=creds)
            return func(service, *args, **kwargs)
        except HttpError as error:
            print(f"An error occurred: {error}")
            return error

    return wrapper


def get_creds() -> Union[Credentials, ExternalCreds]:
    """Return the Google API OAuth credentials."""
    creds = None
    # The file token.json stores the user's access and refresh tokens, and is
    # created automatically when the authorization flow completes for the first
    # time.
    if os.path.exists("token.json"):
        creds = Credentials.from_authorized_user_file("token.json", SCOPES)
    # If there are no (valid) credentials available, let the user log in.
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
            creds = flow.run_local_server(port=0)
        # Save the credentials for the next run
        with open("token.json", "w") as token:
            token.write(creds.to_json())
    return creds


@sheets_api_call
def create(service, title):
    """Creates the Sheet the user has access to."""
    spreadsheet = {"properties": {"title": title}}
    spreadsheet = (
        service.spreadsheets()
        .create(body=spreadsheet, fields="spreadsheetId,spreadsheetUrl,sheets")
        .execute()
    )
    print(f"Spreadsheet URL: {spreadsheet.get('spreadsheetUrl')}")
    write_to_file(spreadsheet)
    return spreadsheet.get("spreadsheetId")


def write_to_file(spreadsheet):
    """Save spreadsheet object in JSON file."""
    with open(SPREADSHEET_FILENAME, "w+") as f:
        json.dump(spreadsheet, f)
        print(f"Wrote spreadsheet data to {SPREADSHEET_FILENAME}")


@sheets_api_call
def update_values(service, spreadsheet_id, range_name, value_input_option, _values):
    """Creates the batch_update the user has access to."""
    body = {"values": _values}
    result = (
        service.spreadsheets()
        .values()
        .update(
            spreadsheetId=spreadsheet_id,
            range=range_name,
            valueInputOption=value_input_option,
            body=body,
        )
        .execute()
    )
    print(f"{result.get('updatedCells')} cells updated.")
    return result


def find_replace_request(find: str, replacement: str, sheet_id: int) -> dict:
    """
    Adds a `findReplace` object for the batchUpdate request.

    Ref:
      - https://developers.google.com/sheets/api/reference/rest/v4/spreadsheets/request#findreplacerequest
    """
    return {
        "findReplace": {
            "find": find,
            "replacement": replacement,
            "sheetId": sheet_id,
        }
    }


def paste_data_request(coordinate: Coordinate, html_data: str) -> dict:
    """
    Adds a `pasteData` object for the batchUpdate request.

    Ref:
      - https://developers.google.com/sheets/api/reference/rest/v4/spreadsheets/request#pastedatarequest
    """
    return {
        "pasteData": {
            "coordinate": asdict(coordinate, dict_factory=camel_case_dict_factory),
            "data": html_data,
            "type": "PASTE_NORMAL",
            "html": True,
        }
    }


def update_cells_request(
    start_coordinate: Coordinate, rows: list[list[dict]], fields: str = "*"
) -> dict:
    """
    Adds an `updateCells` object for the batchUpdate request.

    The `rows` field is comprised a list of rows each holding a list of `CellData`
    objects. See link below for the structure of the `CellData` object.

    Ex:
      cell_data = {
        "textFormatRuns": [
          {
            "startIndex": 0,
            "format": {"foregroundColorStyle": "LINK"},
          },
        ],
      }
      rows = [
        [cell_data]  # a single cell in a single row
      ]

    Ref:
      - https://developers.google.com/sheets/api/reference/rest/v4/spreadsheets/request#UpdateCellsRequest
      - https://developers.google.com/sheets/api/reference/rest/v4/spreadsheets/cells#CellData
    """
    return {
        "updateCells": {
            "start": asdict(start_coordinate, dict_factory=camel_case_dict_factory),
            "fields": fields,
            "rows": rows,
        }
    }


@sheets_api_call
def batch_update(service, spreadsheet_id: str, requests: list[dict[str, str]]):
    body = {"requests": requests}
    return (
        service.spreadsheets()
        .batchUpdate(spreadsheetId=spreadsheet_id, body=body)
        .execute()
    )


@sheets_api_call
def read_cell(service, spreadsheet_id: str, range: str):
    result = (
        service.spreadsheets()
        .values()
        .get(
            spreadsheetId=spreadsheet_id,
            range=range,
        )
        .execute()
    )
    rows = result.get("values", [])
    print(result)
    print(f"{len(rows)} rows retrieved")
    return result


@sheets_api_call
def get_spreadsheet_data(service, spreadsheet_id: str):
    return (
        service.spreadsheets()
        .get(
            spreadsheetId=spreadsheet_id,
            fields="sheets/properties/sheetId,sheets/data/rowData/values",
        )
        .execute()
    )


def get_sheets_from_spreadsheet(spreadsheet) -> list:
    """Return a list of sheets from a spreadsheet response.

    See the structure of the data here:
    https://googleapis.github.io/google-api-python-client/docs/dyn/sheets_v4.spreadsheets.html#get
    """
    # return
    return spreadsheet.get("sheets", [])


def get_ranges_from_sheet(sheet) -> list:
    return sheet.get("data", [])


def get_rows_from_range(cell_range) -> list:
    return cell_range.get("rowData", [])


def get_cells_in_row(row) -> list:
    return row.get("values", [])


def get_exercises() -> list[Exercise]:
    """Give us a list of exercise objects."""
    with open("exercises.json") as f:
        exercises = json.load(f)
    return exercises


def extract_values(response):
    all_values = []
    for sheet in response:
        for data in sheet.get("data", []):
            for row in data.get("rowData", []):
                row_values = []
                for cell in row.get("values", []):
                    formatted_value = cell.get("formattedValue", "")
                    row_values.append(formatted_value)
                all_values.append(row_values)
    return all_values


@dataclass
class Link:
    href: str
    start: int
    end: int
    is_new: bool

    def to_text_format_run(self) -> dict:
        """Turn the `Link` into a `TextFormatRun` for the Google Sheets API.

        This method does NOT take into account the length of the hyperlinked string.
        This requires adjusting the `startIndex` of the subsequent `TextFormatRun`.

        Ref:
          - https://developers.google.com/sheets/api/reference/rest/v4/spreadsheets/cells#TextFormatRun
        """
        return {
            "startIndex": self.start,
            "format": {
                "foregroundColorStyle": {"themeColor": "LINK"},
                "underline": True,
                "link": {"uri": self.href},
            },
        }


def handle_textFormatRun(cell: dict, run: dict, idx: int) -> Link | None:
    if "format" in run and "link" in run["format"]:
        if idx >= len(cell["textFormatRuns"]) - 1:
            # this is the last run
            return Link(
                href=run["format"]["link"].get("uri", ""),
                start=run.get("startIndex", 0),
                end=len(cell.get("formattedValue", "")),
                is_new=False,
            )

        # there is a next run
        next_run = cell["textFormatRuns"][idx + 1]
        return Link(
            href=run["format"]["link"].get("uri", ""),
            start=run.get("startIndex", 0),
            end=next_run.get("startIndex"),
            is_new=False,
        )


def process_row_values(row: list[dict]) -> list[list[dict]]:
    row_values = []
    row_links = []
    for cell in row:
        formatted_value = cell.get("formattedValue", "")
        row_values.append(formatted_value)

        links = []
        if (
            "userEnteredFormat" in cell
            and "textFormat" in cell["userEnteredFormat"]
            and "link" in cell["userEnteredFormat"]["textFormat"]
        ):
            # The entire cell is a link
            # e.g., Google link
            uri = cell["userEnteredFormat"]["textFormat"]["link"].get("uri", "")
            links.append(
                Link(
                    href=uri,
                    start=0,
                    end=len(formatted_value),
                    is_new=False,
                )
            )
        elif (
            "effectiveFormat" in cell
            and "textFormat" in cell["effectiveFormat"]
            and "link" in cell["effectiveFormat"]["textFormat"]
        ):
            # Not sure?
            uri = cell["effectiveFormat"]["textFormat"]["link"].get("uri", "")
            links.append(
                Link(
                    href=uri,
                    start=0,
                    end=len(formatted_value),
                    is_new=False,
                )
            )
        elif "textFormatRuns" in cell:
            # There may be multiple links
            for idx in range(len(cell["textFormatRuns"])):
                run = cell["textFormatRuns"][idx]
                link = handle_textFormatRun(cell, run, idx)
                if link is not None:
                    links.append(link)
        row_links.append(links)
    return row_values, row_links


def process_row_into_cells(row: list[dict]) -> list["CellData"]:
    cell_data_row = []
    for cell_dict in row:
        text_format_runs: list[TextFormatRun] = []

        cell_links = []
        if (
            "userEnteredFormat" in cell_dict
            and "textFormat" in cell_dict["userEnteredFormat"]
            and "link" in cell_dict["userEnteredFormat"]["textFormat"]
        ):
            # The entire cell is a link
            # e.g., Google link
            uri = cell_dict["userEnteredFormat"]["textFormat"]["link"].get("uri", "")

            text_format_runs.append(
                TextFormatRun(
                    format=TextFormat(
                        foreground_color_style=ColorStyle(
                            theme_color=ThemeColorType.LINK
                        ),
                        underline=True,
                        link=GoogleAPILink(uri=uri),
                    ),
                )
            )

        elif (
            "effectiveFormat" in cell_dict
            and "textFormat" in cell_dict["effectiveFormat"]
            and "link" in cell_dict["effectiveFormat"]["textFormat"]
        ):
            # Not sure?
            uri = cell_dict["effectiveFormat"]["textFormat"]["link"].get("uri", "")

            text_format_runs.append(
                TextFormatRun(
                    format=TextFormat(
                        foreground_color_style=ColorStyle(
                            theme_color=ThemeColorType.LINK
                        ),
                        underline=True,
                        link=GoogleAPILink(uri=uri),
                    ),
                )
            )

        elif "textFormatRuns" in cell_dict:
            # There may be multiple links
            for run in cell_dict["textFormatRuns"]:
                if "format" in run:
                    if "foregroundColorStyle" in run["format"]:
                        if "rgbColor" in run["format"]["foregroundColorStyle"]:
                            rgba = run["format"]["foregroundColorStyle"]["rgbColor"]
                            foreground_color_style = ColorStyle(rgb_color=Color(**rgba))
                        elif "themeColor" in run["format"]["foregroundColorStyle"]:
                            foreground_color_style = ColorStyle(
                                theme_color=theme_color_type_lookup.get(
                                    run["format"]["foregroundColorStyle"]["themeColor"]
                                )
                            )
                    if "link" in run["format"]:
                        uri = run["format"]["link"]["uri"]

                text_format_dict = {
                    "foreground_color_style": foreground_color_style,
                    "underline": run["format"].get("underline", False),
                    "link": GoogleAPILink(uri=uri),
                }
                text_format_runs.append(
                    TextFormatRun(
                        format=TextFormat(**text_format_dict),
                        start_index=run.get("startIndex", 0),
                    )
                )

        # Done parsing, make a cell
        cell_data_row.append(
            CellData(
                value=cell_dict.get("formattedValue", ""),
                text_format_runs=text_format_runs,
            )
        )

        # row_links.append(links)
    return cell_data_row


def extract_values_with_links(sheet) -> list[list[dict]]:
    """Takes in a sheet from an entire spreadsheet API."""
    all_values = []
    all_links = []
    all_cells = []
    for data in sheet.get("data", []):
        for row in data.get("rowData", []):
            cell_data_row = process_row_into_cells(row.get("values"))
            row_values, row_links = process_row_values(row.get("values"))
            all_values.append(row_values)
            all_links.append(row_links)
            all_cells.append(cell_data_row)
    return all_values, all_links, all_cells


def equal_dimension(list_one: list[list], list_two: list[list]) -> bool:
    """Checks that two two-dimensional arrays are of the same size."""
    if len(list_one) != len(list_two):
        return False
    return all(len(list_one[i]) == len(list_two[i]) for i in range(len(list_one)))


@dataclass
class GoogleAPILink:
    uri: str

    def to_google_dict(self) -> dict:
        """Returns Google's expected dictionary structure for writes."""
        return {"uri": self.uri}


class ThemeColorType(Enum):
    THEME_COLOR_TYPE_UNSPECIFIED = "THEME_COLOR_TYPE_UNSPECIFIED"
    TEXT = "TEXT"
    BACKGROUND = "BACKGROUND"
    ACCENT1 = "ACCENT1"
    ACCENT2 = "ACCENT2"
    ACCENT3 = "ACCENT3"
    ACCENT4 = "ACCENT4"
    ACCENT5 = "ACCENT5"
    ACCENT6 = "ACCENT6"
    LINK = "LINK"


theme_color_type_lookup = {member.value: member for member in ThemeColorType}


@dataclass
class Color:
    red: float = 0
    green: float = 0
    blue: float = 0
    alpha: float = None

    def __post_init__(self):
        if not (0 <= self.red <= 1):
            raise ValueError(
                f"Invalid value for red: {self.red}. Must be between 0 and 1."
            )
        if not (0 <= self.green <= 1):
            raise ValueError(
                f"Invalid value for green: {self.green}. Must be between 0 and 1."
            )
        if not (0 <= self.blue <= 1):
            raise ValueError(
                f"Invalid value for blue: {self.blue}. Must be between 0 and 1."
            )
        if self.alpha is not None and not (0 <= self.alpha <= 1):
            raise ValueError(
                f"Invalid value for alpha: {self.alpha}. Must be between 0 and 1."
            )

    def to_google_dict(self) -> dict:
        """Returns Google's expected dictionary structure for writes."""
        output = {
            "red": self.red,
            "green": self.green,
            "blue": self.blue,
        }
        if self.alpha is not None:
            output["alpha"] = self.alpha
        return output


@dataclass
class ColorStyle:
    rgb_color: Optional[Color] = None
    theme_color: Optional[ThemeColorType] = None

    def __post_init__(self):
        if (self.rgb_color is None and self.theme_color is None) or (
            self.rgb_color is not None and self.theme_color is not None
        ):
            raise ValueError("Must supply only one of rgb_color or theme_color.")

    def to_google_dict(self) -> dict:
        """Returns Google's expected dictionary structure for writes."""
        if self.rgb_color:
            return {"rgbColor": self.rgb_color.to_google_dict()}
        return {"themeColor": self.theme_color.value} if self.theme_color else {}


@dataclass
class TextFormat:
    foreground_color_style: ColorStyle
    underline: Optional[bool] = False
    link: Optional[GoogleAPILink] = None

    def to_google_dict(self) -> dict:
        """Returns Google's expected dictionary structure for writes."""
        output = {
            "foregroundColorStyle": self.foreground_color_style.to_google_dict(),
        }
        if self.underline:
            output["underline"] = True
        if self.link:
            output["link"] = self.link.to_google_dict()
        return output


@dataclass
class TextFormatRun:
    format: TextFormat
    start_index: int = 0

    def to_google_dict(self) -> dict:
        """Returns Google's expected dictionary structure for writes."""
        return {
            "startIndex": self.start_index,
            "format": self.format.to_google_dict(),
        }


@dataclass
class CellData:
    value: str = ""
    text_format_runs: list[TextFormatRun] = field(default_factory=list)

    def to_google_dict(self) -> dict:
        """Returns Google's expected dictionary structure for writes."""
        return {
            "userEnteredValue": {"stringValue": self.value},
            "textFormatRuns": [run.to_google_dict() for run in self.text_format_runs],
        }


@dataclass
class RowData:
    values: list[CellData] = field(default_factory=list)

    def to_google_dict(self) -> dict:
        """Returns Google's expected dictionary structure for writes."""
        return {"values": [cell.to_google_dict() for cell in self.values]}


@dataclass
class Cell:
    data: CellData
    is_new: bool = False


def add_exercise_links(cells: list[list[Cell]], exercises: list[Exercise]) -> None:
    """Modifies `cells` to include exercise links and marks them as changed."""
    # for i_row in range(len(cells)):
    for row in cells:
        for cell in row:
            # check for exercise name
            for exercise in exercises:
                if (
                    exercise.get("url")
                    and exercise.get("name")
                    and exercise["name"].lower() in cell.data.value.lower()
                ):
                    # add link
                    start = cell.data.value.lower().index(exercise["name"].lower())
                    end = start + len(exercise["name"])
                    text_format_runs = [
                        # link
                        TextFormatRun(
                            TextFormat(
                                foreground_color_style=ColorStyle(
                                    theme_color=ThemeColorType.LINK
                                ),
                                underline=True,
                                link=GoogleAPILink(exercise["url"]),
                            ),
                            start_index=start,
                        )
                    ]
                    if end < len(cell.data.value):
                        text_format_runs.append(
                            # plain text
                            TextFormatRun(
                                TextFormat(
                                    foreground_color_style=ColorStyle(
                                        theme_color=ThemeColorType.TEXT
                                    ),
                                ),
                                start_index=end,
                            ),
                        )
                    cell.data.text_format_runs.extend(text_format_runs)
                    cell.is_new = True


def create_update_requests(sheet_id: int, cells: list[list[Cell]]) -> list[dict]:
    """Create a list of requests to pass into `batch_update()`."""

    requests = []
    for i_row in range(len(cells)):
        for i_col in range(len(cells[i_row])):
            cell: Cell = cells[i_row][i_col]
            if not cell.is_new:
                continue

            requests.append(
                update_cells_request(
                    Coordinate(sheet_id, i_row, i_col),
                    rows=[RowData(values=[cell.data]).to_google_dict()],
                )
            )

    return requests


def find_and_replace_exercises(spreadsheet, exercises: list[Exercise]):
    """Look for an exercise in the spreadsheet and hyperlink it."""
    # TODO: refactor for faster search: create block of text and find applicable exercises,
    #       then iterate through all the cells

    # setup two dimensional arrays for values and links
    sheets = get_sheets_from_spreadsheet(spreadsheet)

    # TODO: don't hardcode sheet
    # TODO: trim out `values` and `links`
    values, links, cells = extract_values_with_links(sheets[1])

    sheet_id: int = 0  # 0 is the default given by google sheets
    if "properties" in sheets[1] and "sheetId" in sheets[1].get("properties"):
        sheet_id = sheets[1].get("properties").get("sheetId")

    # TODO: can remove when `values` and `links` have been pruned
    if not equal_dimension(values, links):
        raise ValueError("Your two two-dimensional lists are not equal in size")

    # mark cells as old with `False`
    cells_to_update: list[list[Cell]] = [[Cell(cell) for cell in row] for row in cells]

    # add new links for exercises
    add_exercise_links(cells_to_update, exercises)

    # format cells with <a> tags
    requests = create_update_requests(sheet_id, cells_to_update)

    # paste the new contents
    response = batch_update(spreadsheet_id=spreadsheet_id, requests=requests)
    pprint(response, depth=4)

    # perform a single read for the sheet with one call
    # and single write for each cell in a single `batchUpdate`
    return cells_to_update, requests, response


if __name__ == "__main__":
    print("To get a test Sheets file, run `python ./seed_spreadsheet.py`")

    spreadsheet_id = "1B0awnXZ0Cqg7iIPgP_WcWEqFuE8-gu4hvqH9jjg1_qc"
    sheet_id = 0
    spreadsheet = get_spreadsheet_data(spreadsheet_id=spreadsheet_id)
    sheets = spreadsheet.get("sheets", [])

    exercises = get_exercises()
    cells, requests, response = find_and_replace_exercises(spreadsheet, exercises)

    ## test out this find_and_replace_exercise() function
    # while we build it out