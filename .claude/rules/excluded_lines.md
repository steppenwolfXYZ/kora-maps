# Geo Sanity Filter — Excluded Lines Tracker

Status flags:
- ✅ correct — intentionally excluded, no fix needed
- 🐛 bug — should be drawn, needs a fix
- 🔍 unclear — needs investigation
- ⏳ pending — fix planned but not yet implemented

---

## Foreign lines (no Swiss GTFS) — all correct

| Status | Mode | Ref | Name | OSM ID | Notes |
|--------|------|-----|------|--------|-------|
| ✅ | bus | 1 | NAV 1: Chaédo - Télécabine | 3609683 | French resort bus |
| ✅ | bus | 1 | NAV 1: Télécabine - Chaédo | 18901000 | French resort bus |
| ✅ | bus | 2 | NAV 2: Péroua - Tsamandon | 3613275 | French resort bus |
| ✅ | bus | 5 | NAV 5: Nendaz Tourisme - Pracondou | 3613277 | French resort bus |
| ✅ | bus | 5 | NAV 5: Pracondou - Nendaz Tourisme | 18900999 | French resort bus |
| ✅ | bus | 6 | NAV 6: Télécabine - Siviez | 3613237 | French resort bus |
| ✅ | bus | 6 | NAV 6: Siviez - Télécabine | 18899249 | French resort bus |
| ✅ | bus | 4 | Bus 4: Ville-la-Grand → Téléphérique du Salève | 10440736 | French cross-border |
| ✅ | bus | 37 | Bus 37: Mésange → Delle Gare | 12523089 | French cross-border (documented) |
| ✅ | bus | 37 | Bus 37: Delle Gare → Mésange | 12523295 | French cross-border (documented) |
| ✅ | bus | 86 | Bus 86: Annemasse → Presinge | 13766601 | French cross-border |
| ✅ | bus | 86 | Bus 86: Presinge → Annemasse | 13766602 | French cross-border |
| ✅ | bus | A | Bus TAD A: Grande Pièce → Pas-de-l'Échelle | 15001159 | French demand-responsive |
| ✅ | bus | A | Bus TAD A: Pas-de-l'Échelle → Grande Pièce | 15001160 | French demand-responsive |
| ✅ | bus | A | Bus TAD A: Pas-de-l'Échelle → Châtelaine | 15001161 | French demand-responsive |
| ✅ | bus | A | Bus TAD A: Châtelaine → Pas-de-l'Échelle | 15001162 | French demand-responsive |
| ✅ | bus | A60 | Ligne A60: Gex → Bourg-En-Bresse | 4487770 | French cross-border |
| ✅ | bus | 9A | Bus 9A: Konstanz Universität | 109305 | German city bus |
| ✅ | bus | 6 | 6 Breccia | 6379644 | Como, Italy |
| ✅ | bus | 11 | 11 Sagnino | 2661531 | Como, Italy |
| ✅ | bus | 12 | 12 Tavernola | 2597079 | Como, Italy |
| ✅ | bus | 12 | 12 Camerlata | 6389981 | Como, Italy |
| ✅ | train | — | TER: Vallorbe → Pontarlier | 16757011 | French TER |
| ✅ | train | ICE 20 | ICE 20: Basel → Hamburg | 1856647 | German international |
| ✅ | train | ICE 60 | ICE 60: Basel → München | 2890957 | German international |
| ✅ | train | ICE 60 | ICE 60: München → Basel | 6167773 | German international |
| ✅ | train | TGV 519 | TGV 519: Paris → Saint-Gervais | 3989312 | French TGV |
| ✅ | train | TGV 521 | TGV 521: Paris → Évian | 3989313 | French TGV |
| ✅ | regional_bus | 210 | Bus 210: Nauders → Landeck-Zams | 1766396 | Austrian |
| ✅ | regional_bus | 210 | Bus 210: Landeck-Zams → Nauders | 1766506 | Austrian |
| ✅ | regional_bus | 212 | 212 Spiss – Pfunds | 11972354 | Austrian |
| ✅ | regional_bus | 212 | 212 Pfunds – Spiss | 11972356 | Austrian |
| ✅ | regional_bus | 270 | Bus 270: Stilfserjoch → Stilfs | 14383252 | South Tyrol (Italian) |
| ✅ | regional_bus | 270 | Bus 270: Stilfs → Stilfserjoch | 14383253 | South Tyrol (Italian) |
| ✅ | regional_bus | 273 | Bus 273: Mals → Landeck-Zams | 2459150 | Austrian/Italian |
| ✅ | regional_bus | 273 | Bus 273: Landeck-Zams → Mals | 9346448 | Austrian/Italian |
| ✅ | regional_bus | 165 | Bus 165: Lustenau → Gaißau | 15000438 | Austrian (Vorarlberg) |
| ✅ | regional_bus | 520 | Bus 520: Buttikon → Tuggen | 2255765 | No GTFS candidates |
| ✅ | regional_bus | 7325.2 | StadtBus Laufenburg Linie 2 | 1070890 | German (Laufenburg) |

---

## Swiss lines — OSM geometry issue (correctly excluded for now)

The GTFS match is correct but the OSM route geometry is outdated/wrong, so proximity checks fail.

| Status | Mode | Ref | Name | OSM ID | Notes |
|--------|------|-----|------|--------|-------|
| ✅ | bus | 9 | Bus 9: Luzern, Bramberg → Bahnhof | 1385887 | Line renamed in Luzern; OSM not updated |
| ✅ | bus | 9 | Bus 9: Luzern, Bramberg → St. Karli → Bahnhof | 11820575 | Same, variant |

---

## Swiss lines — bugs (should be drawn)

| Status | Mode | Ref | Name | OSM ID | Notes |
|--------|------|-----|------|--------|-------|
| 🐛 | bus | 22 | Bus 22: Belprahon → Moutier | 17227287 | Check 1 passes in diagnostic but pipeline excludes — geometry/bbox mismatch |
| 🐛 | bus | 22 | Bus 22: Moutier → Belprahon | 17227288 | Same |
| 🐛 | bus | 22 | Bus 22: Belprahon → Eschert → Moutier | 17227289 | Same |
| 🐛 | bus | 22 | Bus 22: Moutier → Eschert → Belprahon | 17227290 | Same |

---

## Swiss lines — needs investigation

| Status | Mode | Ref | Name | OSM ID | Notes |
|--------|------|-----|------|--------|-------|
| 🔍 | bus | 25 | Bus 25: Bern, Gäbelbach → Bümpliz | 9981185 | Wrong GTFS candidate; real Bus 25 has different ref? |
| 🔍 | regional_bus | 14 | Bus 14: Heididorf → Maienfeld | 6519733 | Liechtenstein side, partial GTFS |
| 🔍 | regional_bus | 14 | Bus 14: Maienfeld → Balzers | 6519734 | Cross-border to Liechtenstein |
| 🔍 | regional_bus | 14 | Bus 14: Balzers → Maienfeld | 6519735 | Cross-border to Liechtenstein |
| 🔍 | regional_bus | 22 | Bus 22: Belprahon → Moutier | 17227287 | Same as bus/22 above |
| 🔍 | regional_bus | 130 | Bus 130: St-Gingolph → Vouvry | 15031183 | Check 1 passes; OSM extends past route boundary at St-Gingolph end |
| 🔍 | regional_bus | 130 | Bus 130: Vouvry → St-Gingolph | 15031184 | Same |
| 🔍 | regional_bus | 181 | Bus 181: Dornbirn → Koblach | 3340286 | Vorarlberg; 2/5 on both checks, borderline |
| 🔍 | regional_bus | 212 | Bus 212: Wolhusen → Malters | 11836009 | OSM geometry and GTFS route differ strongly |
| 🔍 | regional_bus | 212 | Bus 212: Malters → Wolhusen | 11836010 | Same |
| 🔍 | regional_bus | 624 | Bus 624: St. Gallenkappel → Walde SG | 12043585 | Wrong GTFS candidate entirely |
| 🔍 | regional_bus | 624 | Bus 624: Walde SG → St. Gallenkappel | 12043586 | Same |
| 🔍 | regional_bus | 625 | Bus 625: Mauborget → Couvet | 12664155 | Regression: was resolved; check1 passes in diagnostic but pipeline excludes |
| 🔍 | regional_bus | 625 | Bus 625: Couvet → Mauborget | 12664222 | Same |
| 🔍 | regional_bus | 661 | Bus 661: Finstersee → Menzingen | 2016170 | Wrong GTFS candidate |
| 🔍 | regional_bus | 661 | Bus 661: Menzingen → Finstersee | 2016171 | Same |
| 🔍 | regional_bus | 83 | Bus 83: Etzelwil → Büron | 20342465 | Wrong GTFS candidate |
| 🔍 | regional_bus | 83 | Bus 83: Büron → Etzelwil | 20342466 | Same |
| 🔍 | train | R5/S5 | R5/S5: St. Margrethen → Feldkirch | 13544807 | Only 2 GTFS stops — threshold too strict for tiny candidates |
| 🔍 | train | R5/S5 | R5/S5: Feldkirch → St. Margrethen | 15050085 | Same |
