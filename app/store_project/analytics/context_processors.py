import os


def google_analytics(request):
    return {
        "GOOGLE_ANALYTICS_GTAG_PROPERTY_ID": os.environ.get(
            "GOOGLE_ANALYTICS_GTAG_PROPERTY_ID", "G-P3D6WNNZQP")
    }
