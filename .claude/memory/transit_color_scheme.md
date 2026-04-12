---
name: Transit Color Scheme
description: Planned color scheme for transit lines, categories, and encoding rules
type: project
---

Transit layer is planned as the next major feature after basemap v1.

## Mode categories and colors

| Mode | Color | Notes |
|---|---|---|
| Train (all rail) | red | one color for all train types; speed shown via thickness |
| Tram | purple | |
| City bus | blue | |
| Ferry | blue | same as city bus; no geographic overlap |
| Metro | green | reserved for future; urban context, good contrast against warm basemap |
| Long-distance bus | turquoise | |
| Mountain railway | yellow | includes funicular, cable car, rack railway, gondola |
| High-speed train | red (reserved) | same hue as train, just thicker |

## Speed and frequency encoding

- **Line thickness = speed** — faster services are thicker; slower services are hidden at lower zoom levels
- **Color saturation = frequency** — higher frequency = more saturated; clamp minimum saturation so thin+infrequent lines stay identifiable by hue

## Swiss categorization

- S-Bahn / RegioExpress → treated as regional/local train
- InterRegio and above → treated as intercity train

## Basemap color context

Roads are gray (car-only) or red-ish brown (walkable streets) — no conflict with yellow mountain lines.
