# Pilot pack

Everything you need for a free 1-month pilot at a local business: how to install, and the papers
(sign, privacy notice, pilot agreement, processing agreement, DPIA, checklist).

> **These are templates, not legal advice.** I am not a lawyer. Before the first real pilot,
> have the papers checked once by a lawyer or a data protection officer (many IHKs offer a free
> first consultation for founders). Never tell a customer the product is "legally approved"
> or "DSGVO-certified". Say: "built for privacy: no recording, only numbers".

## The papers: make them in 2 minutes

```powershell
copy docs\pilot\pilot.example.yaml pilot_cafe.yaml     # fill in the business, you, dates, cameras
countvision-edge pilot-docs --info pilot_cafe.yaml --out pilot_docs_cafe
start pilot_docs_cafe\index.html                        # open each one, Ctrl+P, print or "Save as PDF"
```

Empty PDFs to fill in by hand: `docs/pilot/blank/`. Files named `pilot_*.yaml` and
`pilot_docs*/` are ignored by git (they contain the customer's details).

| Paper | For whom | Language |
|---|---|---|
| `hinweisschild` – the sign | hang it **before** people enter the camera's view, at eye level | DE + EN |
| `datenschutzhinweis` – full privacy notice (Art. 13 GDPR) | at the till, on their website | DE + EN summary |
| `pilotvereinbarung` – pilot agreement = the business's written consent to the pilot | both sign | DE |
| `avv` – processing agreement (Art. 28 GDPR) with the security measures (TOM) | both sign | DE |
| `dsfa` – short DPIA (Art. 35 GDPR) | the business fills it in | DE |
| `checkliste` – before start / weekly / end | you and the business | DE |

## The legal situation in simple words (Germany, GDPR)

1. **The business is the controller** ("Verantwortlicher"): it decides to count people in its
   shop. **You are the processor** ("Auftragsverarbeiter"): your device processes their camera
   images for them. That is why you sign an **AVV** (processing agreement). Without it, you
   should not start.
2. **Legal basis: legitimate interest** (Art. 6(1)(f) GDPR) – staff planning, waiting times,
   safety limits. Visitors' **consent is not the basis** (you cannot ask every visitor). The
   "consent form" in this pack is the **business's** agreement to the pilot.
   German data protection authorities apply Art. 6(1)(f) to private businesses, not § 4 BDSG
   ([DSK guidance on video surveillance, 2020](https://www.datenschutzkonferenz-online.de/media/oh/20200903_oh_v%C3%BC_dsk.pdf)).
3. **Real-time processing is still processing.** Even without recording, the camera image shows
   people for a moment. So the GDPR applies: sign, privacy notice, AVV, and a DPIA check.
4. **Sign before the camera:** who is responsible, contact, purpose, legal basis, storage, where
   to find more. The DSK guidance lists exactly these points for the first-layer sign.
5. **DPIA (Datenschutz-Folgenabschätzung):** the authorities say a DPIA is required when 2 or more
   of their 9 criteria apply. Watching a publicly accessible area systematically with new (AI)
   technology often meets 2. So: let the business fill in the short DPIA. It is mostly done
   already, because our measures (no recording, no face recognition) are written in.
6. **Employees:** cameras must not be used to check staff performance. Point cameras at the
   entrance and the customer area, not at work places (also not the cashier). If there is a
   works council (Betriebsrat), it must agree **before** the start (§ 87(1) no. 6 BetrVG).
7. **Never film:** toilets, changing rooms, staff rooms, the street or neighbours' property.
8. **Your promises must be true.** Everything in the papers is true for the current software:
   no images stored, preview pixelated by default, no cloud, library telemetry off, numbers
   deleted after 45 days (set `storage.retention_days: 45` – it is in `site.example.yaml`).
   If you change the software (e.g. the cloud in Phase 3), update the papers.

## Install for the pilot

Choose one:

| Pilot device | How | Notes |
|---|---|---|
| Your Windows laptop | `irm https://raw.githubusercontent.com/SamiHotak/countvision/main/install.ps1 \| iex` | Easiest for a first pilot. Laptop must stay on, logged in, plugged in. |
| Linux mini-PC (Intel N100 class, ~€150) | `curl -fsSL https://raw.githubusercontent.com/SamiHotak/countvision/main/install.sh \| sudo bash` | Best for a real pilot: Docker, restarts after power loss, `countvision status`. |
| The business's own PC | same as above, after asking them | Agree in writing who may log in. |

Cost note: both installers are free. A mini-PC is a cost (about €120–200) – only buy one when a
business has said yes.

### Steps on site (about 1–2 hours)

1. **Before:** papers signed (agreement + AVV), DPIA filled in, staff informed, signs printed.
2. **Camera:** use their IP camera if it has RTSP (ask for the camera model; search
   "<model> RTSP URL"), or bring a cheap RTSP camera (Reolink/TP-Link Tapo/Hikvision, ~€40–80,
   only after a yes). Mount it **high, looking down at 30–60°**, so feet are visible.
   Use the camera's **sub stream** (640×360 or 1280×720).
3. **Network:** camera and counting device in the same network. Test the stream in VLC first.
4. **Install** (see table above). Enter the RTSP address when asked.
5. **Draw the line:** Windows: double-click "CountVision" → draw the line across the walkway, at
   least ~1.5 m away from the door, arrow pointing IN → Save. Linux: `sudo countvision setup`.
6. **Check against a hand count:** count by hand for 15 minutes while the device counts. Write
   both numbers in the checklist. If the difference is more than ~10 %, move the line or the
   camera and check again.
7. **Start counting:** Windows: close the web app, double-click "CountVision – count only"
   (autostart at login if you said yes). Linux: it is already running.
8. **Hang the signs** before the camera's view, and put the privacy notice at the till.

### Every week (10 minutes, remotely if possible)

- Status: Windows `countvision-edge health --config edge\configs\local.yaml`; Linux `sudo countvision status`.
- Numbers: `countvision-edge report --db data\countvision.db --csv-dir report_week1` (Linux: `sudo countvision report`).
- Send them a short report (visitors per day, busiest hour, average wait). Note any camera outages.
- Do one 15-minute hand count and write it down: this becomes your **real-world accuracy** number.

### At the end

1. Final report + CSV to the business, and a 20-minute feedback talk (what did they use? what is
   missing? would they pay €29–79 per camera per month?).
2. Delete the data: Windows: delete the `data` folder of the app; Linux:
   `sudo countvision down && sudo rm -rf /opt/countvision/data/*`. Confirm the deletion by e-mail.
3. Take down the signs and the device.

## What the business sees and what it does not

- They get numbers: visitors in/out per hour and day, people inside, queue length, wait times.
- They do **not** get video. There is none. If they want security recordings, that is a
  different product with different rules – say no.
