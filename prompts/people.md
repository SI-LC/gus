You are mapping the people who matter at one target account, so the candidate can reach
the right person when a signal fires. Public information only; no personal emails or phone numbers.

## Candidate
{{PROFILE}}

## Account
{{ACCOUNT}}

## Live roles here (may be empty)
{{JOBS}}

## People already mapped (don't duplicate)
{{KNOWN}}

## Bluesky posts mentioning this company and hiring (may be noise)
{{BLUESKY}}

## Find 2–4 people, in this order of value
1. **Hiring manager or team leader** for the area matching the candidate (and any live role): use the
   job description ("reporting to…"), team and leadership pages, launch posts, conference talks.
2. **Announcers**: anyone who posted about hiring for product in the last ~60 days (X, LinkedIn posts,
   Bluesky, Threads, blogs). Include the post as evidence.
3. **A peer** in a related team, as a route to an intro.
4. **The recruiter** for the candidate's target roles, if public.

Only include people with real, linkable evidence. Never guess a name. If the evidence is old or
indirect, say so and mark confidence low.

Reply with ONLY a JSON array:
[{"name": "...", "role": "Head of Product, Platform", "kind": "product_leader|hiring_manager|announcer|peer_pm|recruiter",
  "platform": "linkedin|x|bluesky|web", "url": "public profile or post",
  "evidence": "what links them to this team or role, with the date if there is one",
  "evidence_date": "YYYY-MM-DD or null", "confidence": "high|medium|low",
  "past_companies": ["previous employers, from their public bio or team page; empty list if unknown, never guess"]}]
