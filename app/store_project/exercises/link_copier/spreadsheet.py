import json
import os.path
from dataclasses import dataclass
from typing import Union

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


def create(title):
    """Creates the Sheet the user has access to."""
    creds = get_creds()
    try:
        service = build("sheets", "v4", credentials=creds)
        spreadsheet = {"properties": {"title": title}}
        spreadsheet = (
            service.spreadsheets()
            .create(body=spreadsheet, fields="spreadsheetId,spreadsheetUrl,sheets")
            .execute()
        )
        print(f"Spreadsheet URL: {spreadsheet.get('spreadsheetUrl')}")
        write_to_file(spreadsheet)
        return spreadsheet.get("spreadsheetId")
    except HttpError as error:
        print(f"An error occurred: {error}")
        return error


def write_to_file(spreadsheet):
    """Save spreadsheet object in JSON file."""
    with open(SPREADSHEET_FILENAME, "w+") as f:
        json.dump(spreadsheet, f)
        print(f"Wrote spreadsheet data to {SPREADSHEET_FILENAME}")


def update_values(spreadsheet_id, range_name, value_input_option, _values):
    """Creates the batch_update the user has access to."""
    creds = get_creds()
    try:
        service = build("sheets", "v4", credentials=creds)
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
    except HttpError as error:
        print(f"An error occurred: {error}")
        return error


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


def batch_update(spreadsheet_id: str, requests: list[dict[str, str]]):
    """Searches for `find` and replaces with `replacement`."""
    creds = get_creds()
    try:
        service = build("sheets", "v4", credentials=creds)
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

    except HttpError as error:
        print(f"An error occurred: {error}")
        return error


def read_cell(spreadsheet_id: str, range: str):
    creds = get_creds()
    try:
        service = build("sheets", "v4", credentials=creds)

        result = (
            service.spreadsheets()
            .values()
            .get(
                spreadsheetId=spreadsheet_id,
                range=range,
                fields="*",
            )
            .execute()
        )
        rows = result.get("values", [])
        print(f"{len(rows)} rows retrieved")
        return result
    except HttpError as error:
        print(f"An error occurred: {error}")
        return error


if __name__ == "__main__":
    print("To get a test Sheets file, run `python ./seed_spreadsheet.py`")
