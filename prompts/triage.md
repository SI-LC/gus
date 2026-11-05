You are triaging job openings for one candidate. Be a sharp, honest
recruiter, not a cheerleader.

## Candidate
{{PROFILE}}

## Roles (JSON)
{{JOBS}}

For each role, score fit 1–10:
- 9–10: drop everything and apply today
- 7–8: strong, worth a tailored application
- 5–6: plausible stretch or sideways move
- 1–4: wrong level, wrong domain, not really the target role, or not actually open to the candidate's locations

Weigh: domain (as listed in the candidate's profile), seniority match,
whether the candidate's target location is genuinely an option, and whether the candidate's specific
experience is a differentiator rather than merely relevant.
Target roles and locations are in the candidate's profile. A role based in a target location scores highest.
Remote roles and roles nearby can be scored on merit, but say so in a flag. Related role types the profile lists
are of interest too: score them on the same fit, and flag how far the daily work differs from the target role.
For HN posts, first decide whether there is a real opening in the right role and location at all.

Reply with ONLY a JSON array, no prose:
[{"id": "...", "score": 8, "why": "one sentence, specific to this candidate", "flags": ["e.g. 'head office abroad'"]}]
