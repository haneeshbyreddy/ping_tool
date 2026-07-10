// Map-view viewport areas: the org picks one in Settings (orgs.map_region) and
// the Map page locks pan/zoom to it. Bounds are [south, west, north, east]
// eyeball bounding boxes — view framing, not survey data; the RegionLock pads
// them anyway. Keys are what the DB stores; never rename one without a
// fallback (an unknown key falls back to all-India).

export interface MapRegion {
  key: string
  name: string
  /** [south, west, north, east]; null = no viewport lock (worldwide) */
  bounds: [number, number, number, number] | null
}

export const DEFAULT_MAP_REGION = "india"

const STATES: Array<[string, string, [number, number, number, number]]> = [
  ["andhra-pradesh", "Andhra Pradesh", [12.5, 76.7, 19.2, 84.9]],
  ["arunachal-pradesh", "Arunachal Pradesh", [26.6, 91.5, 29.5, 97.5]],
  ["assam", "Assam", [24.0, 89.6, 28.2, 96.1]],
  ["bihar", "Bihar", [24.2, 83.3, 27.6, 88.3]],
  ["chhattisgarh", "Chhattisgarh", [17.7, 80.2, 24.2, 84.5]],
  ["goa", "Goa", [14.8, 73.6, 15.9, 74.4]],
  ["gujarat", "Gujarat", [20.0, 68.1, 24.8, 74.6]],
  ["haryana", "Haryana", [27.5, 74.4, 31.0, 77.7]],
  ["himachal-pradesh", "Himachal Pradesh", [30.3, 75.5, 33.3, 79.1]],
  ["jharkhand", "Jharkhand", [21.9, 83.3, 25.4, 88.0]],
  ["karnataka", "Karnataka", [11.5, 74.0, 18.5, 78.7]],
  ["kerala", "Kerala", [8.1, 74.8, 12.9, 77.5]],
  ["madhya-pradesh", "Madhya Pradesh", [21.0, 74.0, 26.9, 82.9]],
  ["maharashtra", "Maharashtra", [15.5, 72.5, 22.1, 81.0]],
  ["manipur", "Manipur", [23.7, 92.9, 25.8, 94.9]],
  ["meghalaya", "Meghalaya", [24.9, 89.7, 26.2, 92.9]],
  ["mizoram", "Mizoram", [21.9, 92.1, 24.6, 93.5]],
  ["nagaland", "Nagaland", [25.1, 93.2, 27.1, 95.3]],
  ["odisha", "Odisha", [17.7, 81.3, 22.7, 87.6]],
  ["punjab", "Punjab", [29.4, 73.8, 32.6, 77.0]],
  ["rajasthan", "Rajasthan", [23.0, 69.4, 30.3, 78.4]],
  ["sikkim", "Sikkim", [27.0, 88.0, 28.2, 89.0]],
  ["tamil-nadu", "Tamil Nadu", [8.0, 76.1, 13.7, 80.4]],
  ["telangana", "Telangana", [15.8, 77.2, 19.9, 81.4]],
  ["tripura", "Tripura", [22.8, 91.0, 24.6, 92.4]],
  ["uttar-pradesh", "Uttar Pradesh", [23.8, 77.0, 30.5, 84.7]],
  ["uttarakhand", "Uttarakhand", [28.6, 77.5, 31.5, 81.1]],
  ["west-bengal", "West Bengal", [21.4, 85.7, 27.3, 90.0]],
]

const UNION_TERRITORIES: Array<[string, string, [number, number, number, number]]> = [
  ["andaman-nicobar", "Andaman & Nicobar", [6.6, 92.1, 13.8, 94.4]],
  ["chandigarh", "Chandigarh", [30.6, 76.6, 30.9, 76.9]],
  ["dadra-nagar-haveli-daman-diu", "Dadra & Nagar Haveli and Daman & Diu", [19.9, 72.7, 20.6, 73.3]],
  ["delhi", "Delhi (NCT)", [28.3, 76.7, 29.0, 77.4]],
  ["jammu-kashmir", "Jammu & Kashmir", [32.2, 73.8, 35.1, 76.9]],
  ["ladakh", "Ladakh", [32.2, 75.8, 36.1, 80.0]],
  ["lakshadweep", "Lakshadweep", [7.9, 71.5, 12.5, 74.1]],
  ["puducherry", "Puducherry", [11.6, 79.5, 12.1, 80.0]],
]

export const MAP_REGIONS: MapRegion[] = [
  { key: "india", name: "All India", bounds: [6.4, 68.0, 35.8, 97.5] },
  ...[...STATES, ...UNION_TERRITORIES]
    .sort((a, b) => a[1].localeCompare(b[1]))
    .map(([key, name, bounds]) => ({ key, name, bounds })),
  { key: "world", name: "Worldwide (no limit)", bounds: null },
]

export function mapRegionOf(key: string | null | undefined): MapRegion {
  return MAP_REGIONS.find((r) => r.key === key)
    ?? MAP_REGIONS.find((r) => r.key === DEFAULT_MAP_REGION)!
}
