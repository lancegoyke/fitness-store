{% autoescape off %}Hi,

{{ athlete_name }} accepted your invite on Mastering Fitness and is ready to
train with you.{% if template_title %} The template you wrote, {{ template_title }}, is ready to start for them: open your roster and press Start {{ template_title }} on their row.{% else %} Open your roster to build their first program.{% endif %}

{{ roster_url }}

Train hard,
Mastering Fitness
{% endautoescape %}
