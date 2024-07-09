import json
import os.path
from dataclasses import dataclass
from typing import Union
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


def find_replace_request(find: str, replacement: str, sheet_id: int):
    """Adds a `findReplace` object for the batchUpdate request."""
    return {
        "findReplace": {
            "find": find,
            "replacement": replacement,
            "sheetId": sheet_id,
        }
    }


def paste_data_request(coordinate: Coordinate, html_data: str):
    """Adds a `pasteData` object for the batchUpdate request."""
    return {
        "pasteData": {
            "coordinate": coordinate,
            "data": html_data,
            "type": "PASTE_NORMAL",
            "html": True,
        }
    }


@sheets_api_call
def batch_update(service, spreadsheet_id: str, requests: list[dict[str, str]]):
    """Searches for `find` and replaces with `replacement`."""
    body = {"requests": requests}
    response = (
        service.spreadsheets()
        .batchUpdate(spreadsheetId=spreadsheet_id, body=body)
        .execute()
    )
    # TODO: log results to the console
    # find_replace_response = response.get("replies")[0].get("findReplace")
    # print(
    #     f"{find_replace_response.get('occurrencesChanged', 0)} replacements made."
    # )
    print(f"Batch update: {response}")
    return response


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


def handle_textFormatRun(cell: dict, run: dict, idx: int) -> Link | None:
    if "format" in run and "link" in run["format"]:
        if idx >= len(cell["textFormatRuns"]) - 1:
            # this is the last run
            return Link(
                href=run["format"]["link"].get("uri", ""),
                start=run.get("startIndex", 0),
                end=len(cell.get("formattedValue", "")),
            )

        # there is a next run
        next_run = cell["textFormatRuns"][idx + 1]
        return Link(
            href=run["format"]["link"].get("uri", ""),
            start=run.get("startIndex", 0),
            end=next_run.get("startIndex"),
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
            uri = cell["userEnteredFormat"]["textFormat"]["link"].get("uri", "")
            links.append(
                Link(
                    href=uri,
                    start=0,
                    end=len(formatted_value),
                )
            )
        elif (
            "effectiveFormat" in cell
            and "textFormat" in cell["effectiveFormat"]
            and "link" in cell["effectiveFormat"]["textFormat"]
        ):
            uri = cell["effectiveFormat"]["textFormat"]["link"].get("uri", "")
            links.append(
                Link(
                    href=uri,
                    start=0,
                    end=len(formatted_value),
                )
            )
        elif "textFormatRuns" in cell:
            for idx in range(len(cell["textFormatRuns"])):
                run = cell["textFormatRuns"][idx]
                link = handle_textFormatRun(cell, run, idx)
                if link is not None:
                    links.append(link)
        row_links.append(links)
    return row_values, row_links


def extract_values_with_links(sheet) -> list[list[dict]]:
    """Takes in a sheet from an entire spreadsheet API."""
    print("hi from extract_values_with_links()")
    all_values = []
    all_links = []
    for data in sheet.get("data", []):
        for row in data.get("rowData", []):
            row_values, row_links = process_row_values(row.get("values"))
            all_values.append(row_values)
            all_links.append(row_links)
    return all_values, all_links


def equal_dimension(list_one: list[list], list_two: list[list]) -> bool:
    """Checks that two two-dimensional arrays are of the same size."""
    if len(list_one) != len(list_two):
        return False
    return all(len(list_one[i]) == len(list_two[i]) for i in range(len(list_one)))


def find_and_replace_exercise(spreadsheet, exercises: list[Exercise]):
    """Look for an exercise in the spreadsheet and hyperlink it."""
    # TODO: refactor for faster search: create block of text and find applicable exercises,
    #       then iterate through all the cells

    # setup two dimensional arrays for values and links
    sheets = get_sheets_from_spreadsheet(spreadsheet)
    values, links = extract_values_with_links(sheets[1])

    if not equal_dimension(values, links):
        raise ValueError("Your two two-dimensional lists are not equal in size")

    # add new links for exercises
    # for cell in spreadsheet
    for i_row in range(len(values)):
        for i_col in range(len(values[i_row])):
            # check for exercise name
            v = values[i_row][i_col]
            for exercise in exercises:
                if (
                    exercise.get("url")
                    and exercise.get("name")
                    and exercise["name"].lower() in v.lower()
                ):
                    # add link
                    start = v.lower().index(exercise["name"].lower())
                    end = start + len(exercise["name"])
                    links[i_row][i_col].append(
                        Link(href=exercise["url"], start=start, end=end)
                    )

    # format cells with <a> tags
    # paste the new contents

    # perform a single read for the sheet with one call
    # and single write for each cell in a single `batchUpdate`
    return values, links


if __name__ == "__main__":
    print("To get a test Sheets file, run `python ./seed_spreadsheet.py`")

    spreadsheet_id = "1B0awnXZ0Cqg7iIPgP_WcWEqFuE8-gu4hvqH9jjg1_qc"
    sheet_id = 0
    spreadsheet = get_spreadsheet_data(spreadsheet_id=spreadsheet_id)
    sheets = spreadsheet.get("sheets", [])

    exercises = get_exercises()
    values, links = find_and_replace_exercise(spreadsheet, exercises)

    ## test out this find_and_replace_exercise() function
    # while we build it out
