from django.shortcuts import render

from .forms import CardioCreateForm


def cardio_create(request):
    submitted = request.GET.get("submit")
    if submitted:
        # display the workout
        form = CardioCreateForm(request.GET)
        mode = request.GET.get("mode")
        duration_minutes: int = int(request.GET.get("duration"))
        protocol = request.GET.get("protocol")
        # Time in seconds from cardio.forms.PROTOCOL_CHOICES
        time_unit: str = "second"
        warm_up_minutes: int = 3
        cool_down_minutes: int = 3
        work_seconds: int = 0
        rest_seconds: int = 0

        # LSD activity
        if protocol == "cont":
            work_seconds = 0
            rest_seconds = 0
            num_of_rounds = 0

            # Throttle warm up and cool down based on duration
            if duration_minutes <= 5:
                warm_up_minutes = 0
                cool_down_minutes = 0
            elif duration_minutes <= 10:
                warm_up_minutes = 2
                cool_down_minutes = 0
            elif duration_minutes <= 20:
                warm_up_minutes = 3
                cool_down_minutes = 3
            else:
                warm_up_minutes = 5
                cool_down_minutes = 5

            minutes_under_duress = (
                duration_minutes - warm_up_minutes - cool_down_minutes
            )

        # Intervals
        else:
            # get slice from PROTOCOL_CHOICES str
            # str length must be divisible by two
            work_seconds = int(protocol[: int(len(protocol) / 2)])
            rest_seconds = int(protocol[int(len(protocol) / 2):])
            duration_of_round_seconds = work_seconds + rest_seconds
            minutes_under_duress = (
                duration_minutes - warm_up_minutes - cool_down_minutes
            )  # in minutes

            if duration_of_round_seconds / 60 > minutes_under_duress:
                warm_up_minutes = 0
                cool_down_minutes = 0
                # Recalculate time_under_duress
                minutes_under_duress = (
                    duration_minutes - warm_up_minutes - cool_down_minutes
                )

            # Number of rounds should be a whole number
            num_of_rounds = int(
                minutes_under_duress / (duration_of_round_seconds / 60)
            )  # whole number

            # Disperse leftover time into warm up and cool down
            leftover_minutes = minutes_under_duress % (
                duration_of_round_seconds / 60
            )
            if (leftover_minutes / 2).is_integer():
                warm_up_minutes += int(leftover_minutes / 2)
                cool_down_minutes += int(leftover_minutes / 2)
            else:
                warm_up_minutes += int(leftover_minutes / 2) + 1
                cool_down_minutes += int(leftover_minutes / 2)

            # Turn lotsa seconds into minutes
            if work_seconds and rest_seconds >= 60:
                work_seconds = int(work_seconds / 60)
                rest_seconds = int(rest_seconds / 60)
                time_unit = "minute"

        # For the template
        context = {
            "form": form,
            "submitted": submitted,
            "mode": mode,
            "num_of_rounds": num_of_rounds,
            "duration": duration_minutes,
            "time_under_duress": minutes_under_duress,
            "protocol": protocol,
            "time_unit": time_unit,
            "work": work_seconds,
            "rest": rest_seconds,
            "warm_up": warm_up_minutes,
            "cool_down": cool_down_minutes,
        }
        return render(request, "cardio/new.html", context)

    # Else display blank form
    form = CardioCreateForm()
    return render(
        request,
        "cardio/new.html",
        {"form": form, "submitted": submitted}
    )
