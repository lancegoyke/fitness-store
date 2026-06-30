Heads up — for {{ month_label }}, {{ count }} paying coach{{ count|pluralize:",es" }}
spent more than {{ threshold_pct }}% of their plan revenue on the AI agent:
{% for row in rows %}
  - {{ row.label }} ({{ row.billing_status }}, {{ row.seats }} seat{{ row.seats|pluralize }}):
  {{ row.runs }} run{{ row.runs|pluralize }} · cost ${{ row.cost }} of ${{ row.revenue }} revenue
  ({{ row.ratio_pct }}%) · margin ${{ row.margin }}
{% endfor %}
Estimated cost is the internal per-run estimate; the Anthropic invoice is
authoritative. Run `manage.py meso_agent_usage_report --month {{ month_label }}` for
the full per-coach and per-client breakdown.

If margin keeps compressing, the two levers are dropping MESO_AGENT_MODEL to a
cheaper tier or metering paid agent runs.

Mastering Fitness
