# [P1] A cached reconnect reading can be counted as a meter reset

With **Require all entities** enabled, the recovery timer accepts a valid energy reading after it stays unchanged for one minute. If a `total_increasing` sensor reconnects with a cached reading below its last trusted value, the timer confirms that reading. The accumulator interprets the decrease as a reset and counts the full cached value as consumption since the reset. In a difference group, that amount is subtracted from the group total. The error persists after the current reading arrives. See [the timeout confirmation](../custom_components/electric_groups/sensor.py#L427) and [the reset calculation](../custom_components/electric_groups/energy.py#L96).

## Example

The base entity measures total consumption. The difference group subtracts one member, a Shelly energy entity. The group starts at **0 kWh**, with initial meter readings of **100 kWh** for the base and **40 kWh** for the member. The test deliberately supplies an older **39 kWh** reading on reconnection. This is a simulated input whose origin is unspecified.

| Time | Base entity | Entity to subtract | Group sensor with the patch | Group sensor with the proposed guard |
| --- | ---: | ---: | ---: | ---: |
| 12:00 · Group starts | 100 kWh | 40 kWh | 0 kWh | 0 kWh |
| 12:01 · Member goes offline | 100 kWh | Unavailable | Unavailable | Unavailable |
| 12:10 · Base keeps counting | 110 kWh | Unavailable | Unavailable | Unavailable |
| 12:11 · Member reconnects | 110 kWh | 39 kWh (cached) | Unavailable | Unavailable |
| 12:12 · One minute without a new report | 110 kWh | 39 kWh (unchanged) | **−29 kWh** | Unavailable |
| 12:13 · Member reports its current reading | 110 kWh | 42 kWh | −29 kWh | **8 kWh** |
| 12:14 · Base reports again | 111 kWh | 42 kWh | **−31 kWh** | **9 kWh** |

At 12:12, the base has added **10 kWh**. The patch treats the cached **39 kWh** as consumption after a reset, so the group shows **10 − 39 = −29 kWh**. When the member reports **42 kWh**, the patch subtracts another **3 kWh**. At 12:14 the base has added **11 kWh** in total and the patch has subtracted **42 kWh**, giving **−31 kWh**.

The correct member consumption is **42 − 40 = 2 kWh**. With **11 kWh** from the base, the group should show **11 − 2 = 9 kWh**.

## Proposed fix

Keep the last trusted meter reading when a reconnect value would trigger a reset. Require a fresh report from that energy entity before applying the reset calculation. The one-minute rule can still confirm stable readings that do not trigger a reset. A possible reset can keep the group sensor unavailable if that entity never reports again.

## Review verification

The reviewer ran all 14 existing tests and the Ruff checks successfully. A focused test for this scenario passed on the base commit with **9 kWh** and failed on the patch with **−31 kWh**.
