"""VoltDrop deal bot — finds, vets, and posts electronics/PC-parts deals.

Structure:
- config: environment variables and tuning constants
- sources: deal fetchers (Woot, Best Buy, Steam)
- storage: Supabase persistence (dedupe state, price history, run log)
- integrations: Discord and Bluesky posting
- ai: OpenRouter-backed captions, spec extraction, classification
- pipeline: the orchestrator that ties it all together
"""