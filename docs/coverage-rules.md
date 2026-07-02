# Coverage Rules — MGH Anesthesia 5PM Coverage Planner

This document describes the domain rules the planner enforces, as encoded in
`reliefplan/zones.py`, `reliefplan/models.py`, `reliefplan/loader.py`,
`reliefplan/algorithm.py`, and the LLM system prompt in `reliefplan/parser.py`.
It is written for anesthesia coordinators and new developers. Every number is
taken directly from the code; the source symbol is cited in parentheses.

## 1. Floor and building topology

The OR map lives in `zones.FLOOR_ORS`. Each OR belongs to exactly one floor,
and each floor to one building (`zones.OR_TO_FLOOR`, `zones.OR_TO_BUILDING`).

| Floor   | Building | ORs |
|---------|----------|-----|
| THOR    | Legacy   | 4, 14, 15, 16, 43, 44 |
| Gray    | Legacy   | 5, 6, 7, 10, 11 |
| Jackson | Legacy   | 12, 32–42, 48 |
| L2      | Lunder   | 51–54 |
| L3      | Lunder   | 61–73 |
| L4      | Lunder   | 17, 81–91 |
| IR      | IR (offsite) | 201–209 (virtual IDs for evening IR rooms) |
| Endo    | Endo (offsite) | 211–219 (virtual IDs for evening Endo rooms) |

Rules a single supervising attending must obey:

- **One building only.** Once an attending is locked to a building, every
  additional room must be in that building (`_AttState.can_supervise`). An
  attending covering both Legacy and Lunder is flagged as a hard VIOLATION
  error in the verification pass (algorithm step 7c).
- **Legacy floors mix freely.** THOR, Gray, and Jackson are one building, so
  any combination is allowed (`zones.same_building`,
  `_identify_regions` treats all of Legacy as one region).
- **Lunder rule (the "L2+L4 without L3" rule).** Within Lunder, an attending
  may cover L2+L3, L3+L4, or L2+L3+L4, but **never L2 and L4 together without
  also covering L3** (`zones.floors_compatible`, checked in
  `_AttState.can_supervise` and verified in step 7d). Intuition: L3 is the
  connecting floor; skipping it means the attending cannot move between rooms
  quickly.
- **IR/Endo are isolated offsite locations.** Each is its own "building";
  an attending may not mix offsite (IR/Endo) rooms with main-OR rooms
  (swap-pass feasibility check in `_swap_improvement_pass`; verified as a
  VIOLATION error in step 7c). Offsite rooms are only staffed by *surplus*
  attendings after all main-OR rooms are covered (the optional offsite pass in
  `plan`); if none is free, an info note says the offsite team will finish
  their own case.

## 2. Roles and shift types

Valid roles are `attending`, `CRNA`, `resident` (`loader.VALID_ROLES`).
Valid shift types (`loader.VALID_SHIFT_TYPES`):

| Shift type | Role | Meaning in the algorithm |
|------------|------|--------------------------|
| `1-A` | attending | Late attending reserved to pair 1:1 with the R1 resident for emergency cases. Never assigned solo (`_AttState.can_go_solo`); last choice for supervision and heavily penalized per room. |
| `2-A` | attending | Second late attending. Never assigned solo; preferred supervisor after 3p-10p, with a small bonus while under 3 rooms. |
| `7a-7p` | attending | Day-into-evening attending; mid-priority supervisor, first regular choice for solo rooms. |
| `3p-10p` | attending | Evening attending; **first choice** for supervision (+15 score bonus) and a preferred cover for cases running 9pm or later. |
| `moonlighter` | attending | Extra hired attending; **first choice for solo rooms** (+40 solo bonus), deprioritized for supervision (−20). |
| `CRNA-7a-8p` | CRNA | Day CRNA staying to 8pm; +40 continuity bonus when kept in their own daytime room (`_find_crna`). |
| `CRNA-5p-8p` | CRNA | Evening-only CRNA pool. |

Resident levels are `R2`, `R3`, `R4` (`loader.VALID_RESIDENT_LEVELS`). R2s are
steered to complex rooms (+40 in `_find_resident`), R3/R4 to routine rooms
(+20). The provider-type decision (`_decide_provider_types`) only designates a
room "resident-first" for a complex case if an R2 is in budget.

Key `StaffMember` fields (`models.StaffMember`):

- `already_deployed` — a 2-A/3p-10p attending already physically placed in a
  room before 5pm. They get a dedicated continuity pass (kept supervising
  their `daytime_or` first) plus a +60 score bonus, and their room anchors its
  region's provider-type choice.
- `daytime_or` — the OR this person worked during the day; drives all
  continuity bonuses.
- `departure_target` — a display-only planned leave time (e.g. "22:00"); the
  algorithm does not act on it.
- `restrictions` — e.g. `no-fluoro`; `affinities` — floors/buildings the
  person tends to work (e.g. `L3`).
- `daytime_location = "EP"` marks CRNAs tied up in EP until ~5:15pm; they are
  assigned last (−60 in `_find_crna`).

## 3. The algorithm, step by step (`algorithm.plan`)

1. **Identify relief needs.** For every late-running room, any daytime
   attending/CRNA/resident who is *not* on the available-past-5pm staff list
   becomes a `ReliefEntry` — someone the evening team must relieve.
2. **(Step 1.5) Region-first provider-type decision**
   (`_decide_provider_types`). Before naming anyone, each late room is
   designated CRNA / resident / solo. Rooms are grouped into geographic
   regions (`_identify_regions`: all Legacy floors as one region, each Lunder
   floor its own, IR/Endo separate); regions are processed largest first so
   big clusters consume the CRNA pool. Complex rooms take an R2 if available
   and no CRNA-continuity relationship would be broken; otherwise CRNA, then
   resident, then solo as budgets run out. A within-region sanity swap gives
   locked-attending rooms CRNAs (4:1 capacity) over non-locked rooms.
3. **Place CRNAs (step 2).** Continuity rooms first (daytime CRNA still
   available, or a pool CRNA's `daytime_or` matches), complex rooms next.
   Best CRNA per room chosen by `_find_crna` scoring (section 5).
4. **Place residents (step 3)** — only after all CRNAs, complex rooms first
   (`_find_resident` scoring).
5. **Assign supervising attendings (step 4).** A continuity pass first keeps
   `already_deployed` attendings on their daytime room, then a greedy pass
   assigns each remaining supervised room to the feasible attending with the
   highest `_supervision_score`. Rooms are ordered so high-affinity rooms and
   CRNA rooms are handled first; attendings are ordered 3p-10p → 2-A → 7a-7p
   → 1-A → moonlighter (`_SUPERVISION_PRIORITY`).
6. **Assign solo attendings (steps 5–6)** to rooms without a physical
   provider, in order moonlighter → 7a-7p → 2-A → 3p-10p → 1-A
   (`_SOLO_PRIORITY`; 1-A/2-A are excluded by `can_go_solo` and only used as
   an explicitly warned last resort).
7. **Optimization and verification (step 7).** 7a: convert a solo room to
   CRNA-supervised if a free CRNA and a supervisor with capacity exist. 7b:
   report 1-A load (see section 5). 7c/7d: verify no cross-building or
   offsite/main mixing and the Lunder rule. Then the optional offsite pass,
   followed by a **greedy swap-improvement pass** (`_swap_improvement_pass`,
   capped at 20 passes) that exchanges room pairs between attendings when it
   raises total supervision score without breaking any hard constraint.
   Finally soft-preference warnings and stay-late suggestions are emitted.

## 4. Hard constraints

Enforced in `_AttState.can_supervise` / `can_go_solo` and re-checked by the
swap pass; the same numbers appear in the parser system prompt
(`parser.py` "Supervision constraints").

| Constraint | Rule |
|-----------|------|
| Ratio with residents | If an attending supervises **any** resident room: max **2** supervised rooms total. |
| Ratio, CRNAs only | If all supervised rooms have CRNAs: max **4** rooms. |
| Solo exclusivity | A solo attending covers exactly 1 room and supervises nothing (`can_go_solo`, `is_solo` check in `can_supervise`). |
| 1-A / 2-A never solo | `can_go_solo` rejects shift types `1-A` and `2-A`; a warned fallback exists only when no one else can cover. |
| New-start cap | An attending may supervise at most **1** room whose case has not yet started (`is_new_start`, `_AttState.new_start_count`). |
| Fluoro | Staff with the `no-fluoro` restriction can never enter a `flagged_fluoro` room — applies to attendings, CRNAs, and residents alike. |
| One building | Supervision may not span Legacy and Lunder, nor mix IR/Endo with main ORs. |
| Lunder rule | L2 + L4 without L3 is forbidden (`zones.floors_compatible`). |
| Supervised provider | A CRNA or resident must always have an attending assigned to their OR (parser hard rules; unmatched rooms become `error` warnings). |

## 5. Soft preferences and scoring

**Supervision score** (`_supervision_score`; higher = better):

| Factor | Weight | Intent |
|--------|--------|--------|
| Same floor already covered | +50 | Cluster an attending's rooms geographically. |
| Same building already locked | +30 | Ditto, weaker. |
| Attending's `daytime_or` == room | +80 | Continuity. |
| Already deployed **and** in this room | +60 extra | Keep pre-5pm placements (continuity rule 7). |
| Floor affinity / building affinity | +20 / +10 | Personal work patterns. |
| Load balancing | −12 per room already supervised | Spread rooms; weaker than the +50/+30 geography bonuses so geography still wins cross-zone. |
| `3p-10p` shift | +15 | Evening attending is the natural supervisor. |
| `2-A` under 3 rooms | +5 | Slight preference while capacity remains. |
| `1-A` reserve penalty | extra −23 per room (total −35/room with load balancing) | Preserve the 1-A at 1:1 for R1 emergency pairing. |
| Moonlighter | −20 | Moonlighters should go solo, not supervise. |

**Solo score** (`_solo_score`): daytime room match +100, already-deployed in
that room +50, floor/building affinity +20/+10, moonlighter +40.

**CRNA choice** (`_find_crna`): named daytime CRNA of the room +100,
`daytime_or` match +80, `CRNA-7a-8p` in own room +40, floor/building affinity
+20/+10, EP daytime location −60, reserve penalty −70 for stealing a CRNA
whose own continuity room is still waiting. **Resident choice**
(`_find_resident`): named daytime resident +100, `daytime_or` match +80,
R2-to-complex +40, R3/R4-to-routine +20.

**Soft warnings** (not blocking):

- Rooms estimated to end at **9pm or later** (`_ends_late`, threshold hour 21)
  should be covered by a `1-A`, `2-A`, or `3p-10p` attending; anything else
  triggers a "prefer 1-A / 2-A / 3p-10p to minimise handoffs" warning
  (`_soft_preference_warnings`).
- 1-A load report (step 7b): 0 rooms = info (free for R1 emergencies);
  1 room = info (ideal, can expand 2:1 with R1); 2–3 rooms = warning (reduced
  R1 reserve); ≥4 rooms = warning (no reserve capacity).
- Stay-late suggestions: for a still-uncovered room whose daytime attending is
  not on PM staff and supervised only that one late room, suggest asking them
  to stay solo.

## 6. Relief priority ordering

- **CRNAs are placed before residents** (steps 2 vs 3), and CRNA-supervised
  rooms are processed before resident rooms when picking supervisors, because
  residents consume ratio capacity faster (2:1 vs 4:1).
- **Continuity first, pool second** at every level: daytime CRNA/resident/
  attending of a room outranks any pool candidate (+100/+80 bonuses; the
  continuity pass for `already_deployed` attendings runs before the greedy
  pass; the −70 CRNA reserve penalty protects continuity rooms).
- **Supervision order:** `3p-10p` → `2-A` → `7a-7p` → `1-A` → `moonlighter`
  (`_SUPERVISION_PRIORITY`), already-deployed before not.
- **Solo order:** `moonlighter` → `7a-7p` → `2-A` → `3p-10p` → `1-A`
  (`_SOLO_PRIORITY`), with 1-A/2-A allowed only as a warned last resort.
- **Region order:** largest late-room cluster first (Legacy, then big Lunder
  floors) so the CRNA pool serves the biggest clusters; offsite IR/Endo rooms
  are covered last, by surplus attendings only.
