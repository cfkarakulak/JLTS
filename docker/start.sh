#!/bin/bash

echo '
      _ _     ____
     | | |   |___ \
  _  | | |     __) |
 | |_| | |___ / __/
  \___/|_____|_____|

'

# run cron on startup
cron

# supervisor additional config
/usr/bin/supervisord -c /etc/supervisor/supervisord.conf
