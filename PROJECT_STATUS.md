# Projektstatus – PumpSteer

## Utveckling

- Publicerad Pilot-bas är [`review/pilot-development-20261121` på `7c05eee`](https://github.com/TheLordVader/PumpSteer/tree/7c05eee435fa34bed2a2a51b798f92b244cd972e). Den aktuella kandidaten tillför tre granskade kodcommits för källtider, passiv Pilot och reproducerbara offlineprov. Se [Pilotens provbeskrivning](docs/PASSIVE_PILOT_DEMO.md).
- Forkens `main` låg på `bc9ae02` vid granskningen. Avgränsa ändringar i granskningsgrenar och följ [upstream](https://github.com/JohanAlvedal/PumpSteer).
- Kandidaten har syntetiskt testunderlag. Kvalificerade fältintervall och oberoende prognospar återstår.

## Installerad Pilot och styrning

- Senast dokumenterade installerade Pilot-bas återfinns i [`review/installed-20261119` på `81632a4`](https://github.com/TheLordVader/PumpSteer/tree/81632a494063aa5b8e9fc05a6bf2cff69fcab99d). Kontrollera aktuell installerad version separat inför driftgranskning.
- Base med PI ansvarar för styrningen. Pilot-kandidaten är avsedd för passiv granskning. Fältprov och installation kräver uttryckligt GO och verifierad återgång.
