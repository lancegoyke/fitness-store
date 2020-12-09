from django.shortcuts import render

from .forms import CardioCreateForm

# Create your views here.
def cardio_create(request):
    submitted = request.GET.get("submit")
    if submitted:
        # display the workout
        form = CardioCreateForm(request.GET)
        mode = request.GET.get("mode")
        duration = int(request.GET.get("duration"))  # in minutes
        protocol = request.GET.get("protocol")
        # Time in seconds from cardio.forms.PROTOCOL_CHOICES
        time_unit = "second"
        warm_up = 3  # in minutes
        cool_down = 3  # in minutes

        # LSD activity
        if protocol == "cont":
            work = 0
            rest = 0
            num_of_rounds = 0

            # Throttle warm up and cool down based on duration
            if duration <= 5:
                warm_up = 0
                cool_down = 0
            elif duration <= 10:
                warm_up = 2
                cool_down = 0
            elif duration <= 20:
                warm_up = 3
                cool_down = 3
            else:
                warm_up = 5
                cool_down = 5

            time_under_duress = duration - warm_up - cool_down

        # Intervals
        else:
            # get slice from PROTOCOL_CHOICES str
            # str length must be divisible by two
            work = int(protocol[: int(len(protocol) / 2)])  # in seconds
            rest = int(protocol[int(len(protocol) / 2) :])  # in seconds
            duration_of_round = work + rest  # in seconds
            time_under_duress = duration - warm_up - cool_down  # in minutes

            if duration_of_round / 60 > time_under_duress:
                warm_up = 0
                cool_down = 0
                # Recalculate time_under_duress
                time_under_duress = duration - warm_up - cool_down

            # Number of rounds should be a whole number
            num_of_rounds = int(
                time_under_duress / (duration_of_round / 60)
            )  # whole number

            # Disperse leftover time into warm up and cool down
            leftover_time = time_under_duress % (duration_of_round / 60)  # in minutes
            if (leftover_time / 2).is_integer():
                warm_up += int(leftover_time / 2)  # in minutes
                cool_down += int(leftover_time / 2)  # in minutes
            else:
                warm_up += int(leftover_time / 2) + 1  # in minutes
                cool_down += int(leftover_time / 2)  # in minutes

            # Turn lotsa seconds into minutes
            if work and rest >= 60:
                work = int(work / 60)
                rest = int(rest / 60)
                time_unit = "minute"

        # For the template
        context = {
            "form": form,
            "submitted": submitted,
            "mode": mode,
            "num_of_rounds": num_of_rounds,
            "duration": duration,
            "time_under_duress": time_under_duress,
            "protocol": protocol,
            "time_unit": time_unit,
            "work": work,
            "rest": rest,
            "warm_up": warm_up,
            "cool_down": cool_down,
        }
        return render(request, "cardio/new.html", context)

    # Else display blank form
    form = CardioCreateForm()
    return render(request, "cardio/new.html", {"form": form, "submitted": submitted})


# Helper functions
def is_aerobic(protocol):
    if protocol:
        return True
