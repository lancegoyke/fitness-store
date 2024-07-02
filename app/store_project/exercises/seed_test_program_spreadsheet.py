import json
import os.path
from pathlib import Path
from typing import Union

from google.auth.external_account_authorized_user import Credentials as ExternalCreds
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# If modifying these scopes, delete the file token.json.
SCOPES = ["https://www.googleapis.com/auth/drive.file"]

# The ID and range of a sample spreadsheet.
SAMPLE_SPREADSHEET_ID = "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgvE2upms"
SAMPLE_RANGE_NAME = "Class Data!A2:E"

# A local copy of the spreadsheet
SPREADSHEET_FILENAME = "test_spreadsheet.json"


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
            .create(body=spreadsheet, fields="spreadsheetId,spreadsheetUrl")
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


def seed_spreadsheet_with_program(spreadsheet_id):
    """Seeds the spreadsheet with some test cases."""
    # fmt: off
    values = [
        ["",                                                           "Test User Program"],  # noqa: E501
        ["",                                                           "Week 1",             "Week 2", "Week 3", "Week 4", "Coach Notes",                                                  "Athlete Notes",                             "Test Case"],  # noqa: E501
        ["Push Up",                                                    "3 x 10",             "3 x 10", "3 x 12", "3 x 12", "Arms long at the top",                                         "",                                          "Exercise in main column"],  # noqa: E501
        ["A2) Barbell Romanian Deadlift (RDL)",                        "3 x 10",             "3 x 10", "3 x 12", "3 x 12", "",                                                             "Went back to Kettlebell Romanian Deadlift", "Exercise with letter+number order prepended"],  # noqa: E501
        ["A2) Front Squat",                                            "3 x 10",             "3 x 10", "3 x 12", "3 x 12", "Do Crossed Arm Front Squat instead if unable to hold the bar", "Yeah that was tough",                       "Exercise in the notes section"],  # noqa: E501
        ["Single Leg Romanaian Deadlift (SLRDL) aka the Sipping Bird", "3 x 10",             "3 x 10", "3 x 12", "3 x 12", "Stay long throughout",                                         "Feels good man",                            "Exercise with extra text appended"],  # noqa: E501
        ["B) Safety Squat Bar Squat",                                  "3 x 10",             "3 x 10", "3 x 12", "3 x 12", "Stay tall throughout",                                         "That bar is heavy!!",                       "Exercise not in the database"],  # noqa: E501
    ]
    # fmt: on
    return update_values(spreadsheet_id, "A1:H7", "USER_ENTERED", values)


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


if __name__ == "__main__":
    if not Path(SPREADSHEET_FILENAME):
        create("Link Tester Sheet")

    spreadsheet_id: str
    with open(SPREADSHEET_FILENAME) as f:
        spreadsheet_id = json.load(f).get("spreadsheetId")
    seed_spreadsheet_with_program(spreadsheet_id)
