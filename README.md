# JellyWatchWise <a href="https://www.buymeacoffee.com/jokerKP" target="_blank"><img align="right" src="https://cdn.buymeacoffee.com/buttons/v2/default-yellow.png" alt="Buy Me A Coffee" style="height: 45px !important;width: 163px !important;" ></a>

JellyWatchWise is a Python application designed to help you manage and enforce watch time limits
for users accessing your Jellyfin media server. By integrating with the Jellyfin API, the app monitors
users' total watch time and restricts access to media libraries when their defined time limits are exceeded.

<table><tr><td valign="top">

## Features
* **Time limit for each user**

  Define separate time limits for each user to control their viewing habits.

* **Jellyfin integration**

  The app uses the Jellyfin API to monitor and manage user access.
* **GUI Management**

  Simple GUI for configuring time limits without additional logging in.

* **Requires Playback Reporting plugin or Jellystat server**

  Requires the 
  [`Playback Reporting`](https://github.com/jellyfin/jellyfin-plugin-playbackreporting) 
  plugin on your Jellyfin server
  or
  [`Jellystat`](https://github.com/CyferShepard/Jellystat)
  server to retrieve aggregated user playtime.

</td><td>

![screen.png](screen.png)

</td></tr></table>


##  Why a standalone app instead of a Jellyfin plugin?

This project focuses on simplicity and ease of use.
The standalone app includes a GUI for quick access to time limit settings,
allowing you to adjust values without logging into the Jellyfin server.

## How to use it?

### Prerequisites
1. Generate an **API Key** for JellyWatchWise from your Jellyfin admin account.
2. Ensure access to aggregated user statistics with either:
   * having the `Playback Reporting` plugin installed on your Jellyfin server 
      
     (this is default, no additional configuration needed),
     
   * access to your `Jellystat` server
    
     (put your host address and API key to the configuration file).

### Running with Docker
The easiest way to get started is by using Docker:

1. Pull the image

```bash
docker pull macjoker/jelly-watch-wise
```
2. Prepare configuration.

   Create a `config` folder with a `config.yaml` file.
   The file should include at least:
   * `host` - your Jellyfin server address
   * `token` - your Jellyfin API key.

   See [sample config file](https://github.com/Joker-KP/jelly-watch-wise/blob/main/config/config-sample.yaml) in the repository for reference.


3. Run the container

```bash
docker run -d -v ./config:/app/config -t macjoker/jelly-watch-wise
```
The app will also store logs and backups of media folder UUIDs in the specified config folder.

## How does the lock work?
To apply time limits, ensure Enable All Folders is not selected for any user you wish to have limits applied.
Instead, manually select individual media folders available to each user.

**When time limit is exceeded**
the app unselects all media libraries for the user, effectively revoking access. Media currently playing will continue until the end, but no new media can be accessed.

**Immediate playback stop** is also possible.
The app includes buttons to immediately stop playback by disabling the user's account.
All connections of that user are then closed, and no logging in is possible until the account is enabled again.


## Time-left notifications
While a user is watching, the player (e.g. Android TV) shows a toast message when the remaining time drops below
one of the configured thresholds. Because toasts disappear quickly, each notification can be repeated.

```yaml
notifications:
  enabled: true
  warn_at: [20, 10, 5, 2]   # minutes left at which a notification is sent
  repeat: 3                 # toasts sent per notification
  repeat_interval: 5        # seconds between repeated toasts
  timeout: 5000             # milliseconds each toast stays on screen
```
The check runs on every refresh, so a small `polling_interval` (or webhooks) makes the warnings more punctual.


## Stopping playback when time is up
With `stop_active_sessions_on_time_up: true` a running playback is stopped as soon as the limit is exceeded.

```yaml
limits:
  stop_active_sessions_on_time_up: true
  stop_grace_minutes: 3            # an item that ends within this many minutes is not interrupted
```
Every decision is written to the log (console and `config/watchwise.log`): which session was stopped (user, client,
device, item, remaining time) and stops skipped due to the grace period. A stop runs for up to 60 s: while the
session is still playing, a stop command is sent every second.
