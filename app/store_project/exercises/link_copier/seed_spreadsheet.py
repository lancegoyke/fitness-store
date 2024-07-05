import json
from pathlib import Path

from spreadsheet import create
from spreadsheet import update_values


# A local copy of the spreadsheet
SPREADSHEET_FILENAME = "test_spreadsheet.json"


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
        [],
        ["Test Hello World!"],
    ]
    # fmt: on
    return update_values(spreadsheet_id, "A1:H9", "USER_ENTERED", values)


def main():
    if not Path(SPREADSHEET_FILENAME).exists():
        create("Link Tester Sheet")

    spreadsheet: dict
    with open(SPREADSHEET_FILENAME) as f:
        spreadsheet = json.load(f)

    if spreadsheet is not None:
        spreadsheet_id = spreadsheet.get("spreadsheetId")
        sheets = spreadsheet.get("sheets")
        if sheets is not None and len(sheets) > 0:
            sheet_id = sheets[0].get("properties").get("sheetId")
        else:
            print("No sheets found in the spreadsheet.")
    else:
        print("Spreadsheet is None.")

    if spreadsheet_id is not None and sheet_id is not None:
        seed_spreadsheet_with_program(spreadsheet_id)


if __name__ == "__main__":
    main()
