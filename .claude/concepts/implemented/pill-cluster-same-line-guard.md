# Pill cluster same-line guard

## Problem

The pill-clustering step groups stops that fall within a fixed spatial radius (300 m for the rail pool, 50 m for the non-rail pool). At hubs where several GTFS parents represent the same physical place — train station + adjacent cable car / funicular / rack railway, or train forecourt + tram/bus quay — this is correct and is the main reason the spatial radius exists. At many other locations it wrongly fuses two genuinely distinct stations that just happen to sit close together: the Bern Mattelift elevator (top + bottom platform at the same horizontal coordinate), MOB neighbour pairs (Blonay / Prélaz, Fontanivent / Planchamp, Toveyre / Valmont), S-Bahn neighbour stations (Zürich Friesenberg / Schweighof, Glattbrugg / Opfikon), and many adjacent bus stop pairs especially across the border (Konstanz, Dornbirn, Tettnang, Tisis).

The two failure modes share one feature: in every wrong case, both stops are served by the **same line**. Two sequential stops on a line are by definition different stations.

## Requirements

- The pill-clustering step must never place two stops that are served by the same drawn line into the same cluster. "Served by the same line" means both stops appear in the stop sequence of at least one common emitted line feature.
- The rule applies to both clustering pools (rail at 300 m, non-rail at 50 m) and to every mode that participates in pill clustering.
- When the guard blocks a merge that the spatial radius would otherwise have produced, the affected stops remain as separate clusters and render as separate pills.
- Stops that share no line continue to merge at the existing spatial radius. Mountain-railway hubs (train station + cable car / funicular / rack railway) and train + tram/bus forecourts must keep their current combined-pill behavior.
- The guard operates on the post-scoring emitted line set. Lines that were dropped by the frequency / active-days gates do not contribute "same line" evidence.

## Constraints

- A line with only two stops (Mattelift, isolated cable cars and elevators) is the canonical case the guard must protect.
- Cross-mode hubs where the two stops belong to different lines (Zermatt + Zermatt GGB, Klosters Platz + Gotschnabahn, Brig + Brig Bahnhofplatz, Forch + Forch Bahnhof, EPFL + EPFL (bus)) must continue to merge into one pill.
- The guard does not interact with the parent_station merge that follows clustering. Clusters that share a parent_station are still glued together; no current case exists of two stops sharing a parent_station while also appearing together on the same drawn line.
- The guard only blocks merges. It never causes a previously-singleton stop to split, and it never reassigns a stop between pools.
