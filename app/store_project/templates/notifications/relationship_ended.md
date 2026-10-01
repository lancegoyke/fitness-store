{% autoescape off %}Hi{% if greeting_name %} {{ greeting_name }}{% endif %},

{{ coach_name }} has ended your coaching on Meso. Your training history stays in your account.

You can still open your training home any time:

{{ home_url }}

Mastering Fitness
{% if unsubscribe_url %}
--
You're getting this because {{ coach_name }} coached you on Mastering Fitness.
Unsubscribe from training emails: {{ unsubscribe_url }}
{% endif %}{% endautoescape %}
