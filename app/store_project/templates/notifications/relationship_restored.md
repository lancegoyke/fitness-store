{% autoescape off %}Hi{% if greeting_name %} {{ greeting_name }}{% endif %},

You're back with {{ coach_name }}.{% if restored_count == 1 %} {{ restored_programs }} is in your app again.{% elif restored_count %} {{ restored_programs }} are in your app again.{% endif %}

Open your training home any time:

{{ home_url }}

Mastering Fitness
{% if unsubscribe_url %}
--
You're getting this because {{ coach_name }} coaches you on Mastering Fitness.
Unsubscribe from training emails: {{ unsubscribe_url }}
{% endif %}{% endautoescape %}
