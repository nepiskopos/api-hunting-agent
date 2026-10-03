# Information-Disclosure Hunting Agent -- Findings Report

Total findings: 5

## 1. Exposed .env file with database credentials

- **Endpoint:** `GET /.env`
- **Confidence:** high
- **On public challenge list:** False

**Why this is disclosure:** The .env file contains sensitive database credentials including usernames, passwords, and host information that should not be publicly accessible. This could allow an attacker to gain unauthorized access to the application's databases.

**Evidence:**
```
DB_NAME=crapi\nDB_USER=crapi\nDB_PASSWORD=crapi\nDB_HOST=postgresdb\nDB_PORT=5432\nSERVER_PORT=8080\nMONGO_DB_HOST=mongodb\nMONGO_DB_PORT=27017\nMONGO_DB_USER=crapi\nMONGO_DB_PASSWORD=crapi\nMONGO_DB_NAME=crapi\n
```

**Reproduction:**
1. Send GET request to /.env
2. Response returns 200 with full .env contents including DB credentials

## 2. Exposed community posts with user emails and vehicle IDs

- **Endpoint:** `GET /community/api/v2/community/posts/recent`
- **Confidence:** medium
- **On public challenge list:** True

**Why this is disclosure:** The recent posts endpoint exposes user emails and vehicle IDs in the response body, which could be considered sensitive personal information depending on the application's privacy policy. However, since these are public posts, this might be intentional. Let's check for BOLA first.

**Evidence:**
```
{"posts":[{"id":"VguigU9DTrSwxcWFL4m6XN","title":"Title 3","content":"Hello world 3","author":{"nickname":"Robot","email":"robot001@example.com","vehicleid":"4bae9968-ec7f-4de3-a3a0-ba1b2ab5e5e5","profile_pic_url":"","created_at":"2026-10-02T20:07:07.849Z"},"comments":[],"authorid":3,"CreatedAt":"2026-10-02T20:07:07.849Z"},{"id":"opnKUzCwDCLjnkmUUJR24B","title":"Title 2","content":"Hello world 2","author":{"nickname":"Pogba","email":"pogba006@example.com","vehicleid":"cd515c12-0fc1-48ae-8b61-9230b70a845b","profile_pic_url":"","created_at":"2026-10-02T20:07:07.848Z"},"comments":[],"authorid":2,"CreatedAt":"2026-10-02T20:07:07.848Z"},{"id":"Xxznvnq97i3MYDFFFf9VdD","title":"Title 1","content":"Hello world 1","author":{"nickname":"Adam","email":"adam007@example.com","vehicleid":"f89b5f21-7829-45cb-a650-299a61090378","profile_pic_url":"","created_at":"2/"}
```

**Reproduction:**
1. Send GET request to /community/api/v2/community/posts/recent
2. Response returns list of posts with author emails and vehicle IDs

## 3. Verbose error message exposes internal routing details

- **Endpoint:** `GET /identity/api/v2/community/posts/VguigU9DTrSwxcWFL4m6XN`
- **Confidence:** medium
- **On public challenge list:** False

**Why this is disclosure:** The error response exposes internal routing details by revealing that the path was treated as a "static resource" and shows the full internal path structure. This could help an attacker understand the application's architecture and service boundaries.

**Evidence:**
```
{"type":"about:blank","title":"Not Found","status":404,"detail":"No static resource identity/api/v2/community/posts/VguigU9DTrSwxcWFL4m6XN.","instance":"/identity/api/v2/community/posts/VguigU9DTrSwxcWFL4m6XN"}
```

**Reproduction:**
1. Send GET request to /identity/api/v2/community/posts/VguigU9DTrSwxcWFL4m6XN
2. Response returns 404 with detail field showing internal routing information

## 4. Vehicle location endpoint exposes another user's PII and location via BOLA

- **Endpoint:** `GET /identity/api/v2/vehicle/4bae9968-ec7f-4de3-a3a0-ba1b2ab5e5e5/location`
- **Confidence:** high
- **On public challenge list:** True

**Why this is disclosure:** The vehicle location endpoint returns another user's (Robot) full name, email address, and precise GPS coordinates without authorization. The primary account user should only be able to access their own vehicle's location, not other users'. This exposes sensitive personal information including real-time location tracking data.

**Evidence:**
```
{"carId":"4bae9968-ec7f-4de3-a3a0-ba1b2ab5e5e5","vehicleLocation":{"id":3,"latitude":"37.746880","longitude":"-84.301460"},"fullName":"Robot","email":"robot001@example.com"}
```

**Reproduction:**
1. Authenticate as primary account
2. Send GET request to /identity/api/v2/vehicle/4bae9968-ec7f-4de3-a3a0-ba1b2ab5e5e5/location
3. Response returns Robot's full name, email, and GPS coordinates
4. Send GET request to /identity/api/v2/vehicle/cd515c12-0fc1-48ae-8b61-9230b70a845b/location
5. Response returns Pogba's full name, email, and GPS coordinates

## 5. Community post detail endpoint exposes user email addresses and vehicle IDs without authorization

- **Endpoint:** `GET /community/api/v2/community/posts/opnKUzCwDCLjnkmUUJR24B`
- **Confidence:** high
- **On public challenge list:** True

**Why this is disclosure:** The community post detail endpoint returns another user's (Pogba) email address and vehicle ID without authorization. The primary account user should only be able to see public post content, not the author's personal email or vehicle identifier. This exposes sensitive personal information that could be used for phishing or further attacks against the user.

**Evidence:**
```
{"id":"opnKUzCwDCLjnkmUUJR24B","title":"Title 2","content":"Hello world 2","author":{"nickname":"Pogba","email":"pogba006@example.com","vehicleid":"cd515c12-0fc1-48ae-8b61-9230b70a845b","profile_pic_url":"","created_at":"2026-10-02T20:07:07.848Z"},"comments":[],"authorid":2,"CreatedAt":"2026-10-02T20:07:07.848Z"}
```

**Reproduction:**
1. Authenticate as primary account
2. Send GET request to /community/api/v2/community/posts/opnKUzCwDCLjnkmUUJR24B
3. Response returns Pogba's email address (pogba006@example.com) and vehicle ID (cd515c12-0fc1-48ae-8b61-9230b70a845b)

---

```
Token/cost accounting
  LLM calls:          93
  Agent steps:        82
  Prompt tokens:      1215563
  Completion tokens:  30828
  Total tokens:       1246391
  Avg tokens/call:    13402
  Wall clock:         457.9s
```
