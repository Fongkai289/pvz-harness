"""Provider-reported tokens only. Cached input/reasoning are subsets, never added twice."""
FIELDS = ('input_tokens', 'cached_input_tokens', 'output_tokens', 'reasoning_output_tokens', 'total_tokens')
CODEX_FIELDS = ('inputTokens', 'cachedInputTokens', 'outputTokens', 'reasoningOutputTokens', 'totalTokens')


def usage_delta(total, previous=None):
    previous = previous or {}
    result = {}
    for key, source in zip(FIELDS, CODEX_FIELDS):
        value, prior = total.get(source), previous.get(source, 0)
        result[key] = value - prior if type(value) is int and type(prior) is int and value >= prior else None
    return result


def summarize_calls(calls):
    result = {'calls': len(calls), 'complete': True, 'scope': 'Coach only; excludes Planner/Qwen/Jev'}
    for key in FIELDS:
        values = [(call.get('usage') or {}).get(key) for call in calls]
        known = [v for v in values if type(v) is int and v >= 0]
        result[key] = sum(known) if len(known) == len(values) else None
        result['known_' + key] = sum(known)
        if len(known) != len(values): result['complete'] = False
    return result
