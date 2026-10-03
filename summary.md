# Information-Disclosure Hunting Agent -- Run Summary

Findings accepted: 5   |   proposals rejected: 1

1. [HIGH] Exposed .env file with database credentials -- GET /.env (not on public challenge list)
2. [MEDIUM] Exposed community posts with user emails and vehicle IDs -- GET /community/api/v2/community/posts/recent (on public challenge list)
3. [MEDIUM] Verbose error message exposes internal routing details -- GET /identity/api/v2/community/posts/VguigU9DTrSwxcWFL4m6XN (not on public challenge list)
4. [HIGH] Vehicle location endpoint exposes another user's PII and location via BOLA -- GET /identity/api/v2/vehicle/4bae9968-ec7f-4de3-a3a0-ba1b2ab5e5e5/location (on public challenge list)
5. [HIGH] Community post detail endpoint exposes user email addresses and vehicle IDs without authorization -- GET /community/api/v2/community/posts/opnKUzCwDCLjnkmUUJR24B (on public challenge list)
