# World of Warcraft Auction House Addon

Arbitrage searches the auction house for arbitrage opportunities. When it finds them it sets them as favorites in the auction house.

## Cross-Server Arbitrage Finder (External Script)

The `arbitrage_finder.py` script uses the [Undermine Exchange](https://undermine.exchange/) API to find items
listed below vendor sell price across ALL servers in a region — something an in-game addon cannot do.

### Setup

1. Sign in to https://undermine.exchange/ with Patreon (free)
2. Get your API key from the API page
3. Install Python 3 if you don't have it

### Usage

```shell
# Scan all US servers for vendor arbitrage
python arbitrage_finder.py --api-key YOUR_KEY --region us

# Scan EU servers
python arbitrage_finder.py --api-key YOUR_KEY --region eu

# Check specific items
python arbitrage_finder.py --api-key YOUR_KEY --check-items 21877 1529

# Expand the vendor price database (requires free Blizzard API credentials)
python arbitrage_finder.py --api-key YOUR_KEY --fetch-vendor-prices --blizzard-id ID --blizzard-secret SECRET
```

Output shows item ID, AH price, vendor price, profit margin, quantity, and which realms have the deal.

## Development links, tips

https://warcraft.wiki.gg/wiki/World_of_Warcraft_API

Slash commands usable in the chat window
* /console scriptErrors 1
* /reload - reload the UI
* /dump - general debugging
* /etrace - showing events
* /fstack - debugging visible UI elements
* /tableinspect - interactive table inspection

## Single Threaded

The WoW UI appears to be single threaded. We can create callbacks and have those
run whenever, but they still block the main UI thread. Keep the time spent in any
given callback very short.

You can see this effect by watching the UI freeze while the callback is running
if the callback takes any moderate length of time.

## Lua Script Debugging

`C_CVar.SetCVar("scriptErrors", 1)`

`DevTools_Dump(itemKey)`

Get latest UI version number:

```shell
/run print((select(4, GetBuildInfo())))
```
