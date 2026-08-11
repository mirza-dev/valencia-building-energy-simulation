// The EPW and the .ddy are one input, not two.
//
// The engine refuses a pair whose two files describe different stations - the
// gate against a building simulated in one city with equipment sized for
// another - and it re-validates the pair on every settings write.  So changing
// one field on its own is always rejected, whichever side moves first, and a
// project can never leave the climate it was verified on.  Staging both sides
// and sending them in a single activation is what makes the switch possible at
// all; this module holds that decision so it can be tested without a browser.

export type ClimatePairDraft = {
  weather: string
  ddy: string
  ground: string
  mains: string
}

export type ClimatePairPatch = {
  weather_dataset_id: string
  ddy_dataset_id: string
  ground_temperature_c: number | null
  water_mains_temperature_c: number | null
}

export function climatePairChanged(
  draft: Pick<ClimatePairDraft, 'weather' | 'ddy'>,
  active: { weather: string | null; ddy: string | null },
): boolean {
  return draft.weather !== (active.weather ?? '') || draft.ddy !== (active.ddy ?? '')
}

export function climatePairReady(draft: Pick<ClimatePairDraft, 'weather' | 'ddy'>): boolean {
  return draft.weather !== '' && draft.ddy !== ''
}

/** The patch to send, or why it cannot be sent yet. */
export function climatePairPatch(
  draft: ClimatePairDraft,
): { ok: true; patch: ClimatePairPatch } | { ok: false; reason: string } {
  if (!climatePairReady(draft)) {
    return { ok: false, reason: 'Choose both an annual EPW and a design-day file.' }
  }
  const parse = (raw: string) => (raw.trim() === '' ? null : Number(raw))
  const ground = parse(draft.ground)
  const mains = parse(draft.mains)
  if ((ground !== null && Number.isNaN(ground)) || (mains !== null && Number.isNaN(mains))) {
    return { ok: false, reason: 'Enter the site temperatures in °C.' }
  }
  // The declarations travel with the pair: a climate this project was not
  // verified on is refused without a ground temperature, and blur ordering
  // should not decide whether the activation succeeds.
  return {
    ok: true,
    patch: {
      weather_dataset_id: draft.weather,
      ddy_dataset_id: draft.ddy,
      ground_temperature_c: ground,
      water_mains_temperature_c: mains,
    },
  }
}
