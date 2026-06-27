import cPickle
import json
import os

import BigWorld
from debug_utils import LOG_DEBUG, LOG_ERROR
import sys

CONFIG_OPTIONS = {}
try:
    import sys
    this_file = sys._getframe().f_code.co_filename
    path = os.path.normpath(os.path.join(os.path.dirname(this_file), 'config.json'))
    LOG_DEBUG('Loading config from: ' + path)
    with open(path, 'r') as f:
        CONFIG_OPTIONS = json.load(f)
    LOG_DEBUG('Config loaded successfully: ' + str(CONFIG_OPTIONS))
except Exception, e:
    LOG_ERROR('Failed to load config: ' + str(e))

from chat_shared import CHAT_RESPONSES

OFFLINE_SERVER_ADDRESS = 'offline.loc'
OFFLINE_NICKNAME = str(CONFIG_OPTIONS.get('nickname', 'DrWeb7_1'))
OFFLINE_LOGIN = OFFLINE_NICKNAME + '@' + OFFLINE_SERVER_ADDRESS
OFFLINE_PWD = '1'
OFFLINE_DBID = 13028161

OFFLINE_GUI_CTX = cPickle.dumps({
    'databaseID': OFFLINE_DBID,
    'logUXEvents': False,
    'aogasStartedAt': 0,
    'sessionStartedAt': 0,
    'isAogasEnabled': False,
    'collectUiStats': False,
    'isLongDisconnectedFromCenter': False,
}, cPickle.HIGHEST_PROTOCOL)

OFFLINE_SERVER_SETTINGS = {
    'regional_settings': {'starting_day_of_a_new_week': 0, 'starting_time_of_a_new_game_day': 0, 'starting_time_of_a_new_day': 0, 'starting_day_of_a_new_weak': 0},
    'xmpp_enabled': False,
    'xmpp_port': 0,
    'xmpp_host': '',
    'xmpp_muc_enabled': False,
    'xmpp_muc_services': [],
    'xmpp_resource': '',
    'xmpp_bosh_connections': [],
    'xmpp_connections': [],
    'xmpp_alt_connections': [],
    'file_server': {},
    'voipDomain': '',
    'voipUserDomain': ''
}

CHAT_ACTION_DATA = {
    'requestID': None,
    'action': None,
    'actionResponse': CHAT_RESPONSES.internalError.index(),
    'time': 0,
    'sentTime': 0,
    'channel': 0,
    'originator': 0,
    'originatorNickName': '',
    'group': 0,
    'data': {},
    'flags': 0
}

REQUEST_CALLBACK_TIME = 0.5