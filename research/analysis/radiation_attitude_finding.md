# Can a CubeSat reduce radiation exposure by changing attitude?

**Short answer: no, not meaningfully. The geometry forbids it.** This note exists
because "tilt the spacecraft away from the radiation" is an intuitive-sounding
autonomous response that the project owner explicitly asked to have checked rather
than assumed. It does not survive the check.

## The numbers (FACT — from cited primary sources)

From Thomsen et al., NASA Langley, "Decreasing Proton Single Event Effects in CubeSats
with Shielding" and "Shielding Considerations for CubeSat Structures"
([NTRS 20230007190](https://ntrs.nasa.gov/citations/20230007190),
[NTRS 20230010927](https://ntrs.nasa.gov/api/citations/20230010927/downloads/Thomsen_Shielding_Considerations_CubeSat_SSC_X-01.pdf)):

| Quantity | Value |
|---|---|
| Typical CubeSat aluminium wall thickness | 0.204–0.254 cm |
| Corresponding areal density | 0.550–0.686 g/cm² |
| Thin-walled 3U structure, effective shielding | 0.907 g/cm² |
| → minimum proton energy that penetrates it | **36.2 MeV** |
| Heavily shielded reference (Shields-1) | 21.3 g/cm² → ~151 MeV threshold |
| Proton energies that dominate SEE in rad-tolerant parts | **≥100 MeV** |

## Why attitude cannot help (SOURCE-DERIVED INFERENCE)

1. **The dominant particles go through the whole spacecraft regardless.** A standard
   CubeSat wall stops protons below ~36 MeV. The protons that actually cause single-event
   effects in reasonably hardened parts are ≥100 MeV. Those are not stopped by any face
   of the spacecraft, so which face they arrive through is irrelevant.

2. **There is nowhere to hide inside a 1U–3U.** Reorienting only changes *which* mass a
   component sits behind. In a CubeSat the total available mass in any direction is a
   few centimetres of aluminium plus some PCB stack and a battery — order 1–3 g/cm² at
   the very best, against 0.6 g/cm² at worst. Scaling from the NASA figures above, that
   moves the penetration threshold from roughly 36 MeV to perhaps 70 MeV. Still below
   the ≥100 MeV band that matters. Large spacecraft can place sensitive electronics deep
   inside a vault; a CubeSat has no deep inside.

3. **The flux is not a beam.** Trapped protons in the South Atlantic Anomaly have a
   pitch-angle distribution about the local field line and the spacecraft is immersed in
   the flux rather than illuminated from one side. Galactic cosmic rays are essentially
   isotropic and, at GeV energies, unshieldable in a small satellite. Only solar particle
   events have a meaningfully preferred direction, and their significant-fluence energies
   still substantially exceed what a CubeSat wall stops.

4. **The manoeuvre itself has real costs.** Slewing a CubeSat perturbs exactly the things
   that keep it alive: solar array illumination (power), antenna pointing (the comms link
   you need to report the problem), and thermal balance. Trading guaranteed power and
   link margin for a marginal and probably illusory radiation benefit is a bad trade.

**Conclusion (ENGINEERING HYPOTHESIS, well supported):** "change attitude to reduce
radiation exposure" should be explicitly excluded from the recovery action set for this
project. It is not merely unhelpful, it is plausibly harmful.

## What actually does work: orbit-position-predictive safing (FACT)

Position-based, not attitude-based, mitigation is real, standard practice and cheap:

- Hubble Space Telescope suspends science observations during SAA passage.
- The Proportional Counter Array (RXTE) used **uplinked predictions of SAA entry/exit
  times derived from ephemeris**, and instruments reduce voltage on predicted SAA entry.
- NASA and ESA maintain magnetic field models, updated from real satellite data, that
  operators use to predict exposure zones.

Source: search-surfaced summaries of SAA operational practice plus
[NTRS 19960035769](https://ntrs.nasa.gov/citations/19960035769). *(Secondary-source
corroboration; the specific RXTE/PCA detail should be re-checked against the instrument
handbook before being cited as primary in the final report.)*

### Why this matters for our testbed

An SAA-aware mode is genuinely implementable on a student CubeSat and is *evidence-backed*
in a way the attitude idea is not:

- It needs only position knowledge (TLE-propagated or GPS), not attitude authority.
- The action is bounded and reversible: reduce voltage / suspend a sensitive payload /
  increase memory scrubbing rate for a predicted window, then restore.
- It is verifiable on the bench: inject a simulated "predicted SAA entry" and confirm the
  system enters and cleanly exits the protective mode.
- It is *predictive* rather than reactive, which is the one genuinely useful thing an
  onboard system can do about radiation — you cannot detect an SEU before it happens, but
  you can know you are about to fly through the region where they cluster.

Note the honest limit: this reduces the *consequences* of upsets during a known-risky
window. It does not reduce the particle flux, and it does nothing for GCR-induced upsets
outside the SAA.
