Hi {{ athlete_name }},

{{ coach_name }} just delivered {{ week_label }} of "{{ plan_title }}" to your training app.

Open it to see your sessions and log your training:

{{ home_url }}

Train hard,
Mastering Fitness
{% if unsubscribe_url %}
--
You're getting this because {{ coach_name }} coaches you on Mastering Fitness.
Unsubscribe from training-delivery emails: {{ unsubscribe_url }}
{% endif %}
