import os
import signal

import Account
import AccountCommands
import Avatar as AvatarModule
import AvatarInputHandler as AvatarInputHandlerModule
import BigWorld
import account_shared

from ConnectionManager import connectionManager
from GameSessionController import _GameSessionController
from account_helpers.Shop import Shop
from debug_utils import LOG_CURRENT_EXCEPTION, LOG_ERROR
from gui.Scaleform.Login import Login
from gui.Scaleform.gui_items.Vehicle import Vehicle
from helpers.time_utils import _TimeCorrector, _g_instance
from nations import INDICES
from predefined_hosts import g_preDefinedHosts

from gui.mods.offhangar.logging import *
from gui.mods.offhangar.utils import *
from gui.mods.offhangar._constants import *
from gui.mods.offhangar.server import *

def _inject_submodule(mod_name, rel_path):
	"""Inject a .py file as a submodule when the normal import fails (missing .pyc)."""
	import sys, os
	full_name = 'gui.mods.offhangar.' + mod_name
	if full_name in sys.modules:
		return sys.modules[full_name]
	import types
	mod = types.ModuleType(full_name)
	mod.__file__ = rel_path
	sys.modules[full_name] = mod
	try:
		execfile(rel_path, mod.__dict__)
	except Exception:
		del sys.modules[full_name]
		raise
	return mod

def _safe_import_offhangar():
	"""Try normal package imports; fall back to execfile injection if .pyc is missing."""
	import sys, os
	candidates = []
	try:
		_this = __file__
		candidates.append(os.path.join(os.path.dirname(_this), 'offhangar'))
		_abs = os.path.abspath(_this)
		candidates.append(os.path.join(os.path.dirname(_abs), 'offhangar'))
	except Exception:
		pass
	candidates.append(r'res_mods\0.8.2\scripts\client\gui\mods\offhangar')
	candidates.append(r'c:\Games\World_of_Tanks_0.08.02.00.00_EU_0543_SD\res_mods\0.8.2\scripts\client\gui\mods\offhangar')
	submodules = ['data', 'state', 'command_handlers', 'command_router',
	              'session_guards', 'offline_battle', 'offline_battle_stack']
	for name in submodules:
		full = 'gui.mods.offhangar.' + name
		if full not in sys.modules:
			try:
				__import__(full)
				continue
			except ImportError:
				pass
			for _pkg_dir in candidates:
				py_path = os.path.join(_pkg_dir, name + '.py')
				if os.path.exists(py_path):
					try:
						_inject_submodule(name, py_path)
						break
					except Exception:
						pass


_safe_import_offhangar()

try:
	from gui.mods.offhangar.data import getOfflineShopItems
except ImportError:
	import gui.mods.offhangar.data as _data_mod
	getOfflineShopItems = getattr(_data_mod, 'getOfflineShopItems', None)

try:
	from gui.mods.offhangar.session_guards import install_game_session_guards
except ImportError:
	import gui.mods.offhangar.session_guards as _sg_mod
	install_game_session_guards = getattr(_sg_mod, 'install_game_session_guards', lambda: None)

try:
	from gui.mods.offhangar.offline_battle import start_offline_random_from_hangar
except ImportError:
	import gui.mods.offhangar.offline_battle as _ob_mod
	start_offline_random_from_hangar = getattr(_ob_mod, 'start_offline_random_from_hangar', lambda *a, **k: None)


Account.LOG_DEBUG = LOG_DEBUG
Account.LOG_NOTE = LOG_NOTE
Account.LOG_ERROR = LOG_ERROR

g_preDefinedHosts._hosts.append(g_preDefinedHosts._makeHostItem(OFFLINE_SERVER_ADDRESS, OFFLINE_SERVER_ADDRESS, OFFLINE_SERVER_ADDRESS))


class _OfflineArenaStub(object):
	class _VehicleTypeStub(object):
		def __init__(self):
			self.type = self
			self.tags = set()
			self.turretRotatorSpeed = 0.0
			self.circularVisionRadius = 0

		def __getattr__(self, name):
			return 0

	class _ArenaTypeStub(object):
		def __init__(self):
			self.weatherPresets = []
			self.geometryName = ''
			self.gameplayName = ''
			self.umbraEnabled = 0
			self.boundingBox = ( (0,0), (1000, 1000) )
			self.defaultReverbPreset = ''
			self.waterTexScale = 0.5
			self.waterFreqX = 1.0
			self.waterFreqZ = 1.0
			self.minimap = None

	class _EventStub(object):
		def __iadd__(self, other):
			return self

		def __isub__(self, other):
			return self

		def __call__(self, *args, **kwargs):
			return

	def __init__(self):
		self.vehicles = {}
		self.statistics = {}
		self.arenaType = self._ArenaTypeStub()
		self.guiType = 0
		self.bonusType = 0
		self.extraData = {}
		self._event_stubs = {}
		self.period = 1
		self.periodLength = 600
		# periodEndTime is NOT set here - __getattr__ returns serverTime()+600 lazily

	def __getattr__(self, name):
		if name == 'periodEndTime':
			try:
				return BigWorld.serverTime() + 600
			except Exception:
				return 0
		if name.startswith('on'):
			if name not in self._event_stubs:
				self._event_stubs[name] = self._EventStub()
			return self._event_stubs[name]
		return 0


class _OfflineVehicleStub(object):
	class _TypeDescriptorStub(object):
		def __init__(self):
			self.type = _OfflineArenaStub._VehicleTypeStub()

		def __getattr__(self, name):
			return 0

	def __init__(self):
		self.typeDescriptor = self._TypeDescriptorStub()
		self.id = 0


class _OfflineEvent(object):
	def __iadd__(self, other):
		return self

	def __isub__(self, other):
		return self

	def __call__(self, *args, **kwargs):
		return


def _ensure_postmortem_event(obj):
	if obj is None:
		return
	try:
		cur = getattr(obj, 'onPostmortemVehicleChanged', None)
		if cur is None or callable(cur):
			obj.onPostmortemVehicleChanged = _OfflineEvent()
	except Exception:
		LOG_CURRENT_EXCEPTION()


def fini():
	os.kill(os.getpid(), signal.SIGTERM)

@override(Shop, '__onSyncComplete')
def Shop__onSyncComplete(baseFunc, baseSelf, syncID, data):
	data = {
		'berthsPrices': (16, 16, [300]),
		'freeXPConversion': (25, 1),
		'dropSkillsCost': {
			0: {'xpReuseFraction': 0.5, 'gold': 0, 'credits': 0},
			1: {'xpReuseFraction': 0.75, 'gold': 0, 'credits': 20000},
			2: {'xpReuseFraction': 1.0, 'gold': 200, 'credits': 0}
		},
		'refSystem': {
			'maxNumberOfReferrals': 50,
			'posByXPinTeam': 10,
			'maxReferralXPPool': 350000,
			'periods': [(24, 3.0), (168, 2.0), (876000, 1.5)]
		},
		'playerEmblemCost': {
			0: (15, True),
			30: (6000, False),
			7: (1500, False)
		},
		'premiumCost': {
			1: 250,
			3: 650,
			7: 1250,
			30: 2500,
			180: 13500,
			360: 24000
		},
		'winXPFactorMode': 0,
		'sellPriceModif': 0.5,
		'passportChangeCost': 50,
		'exchangeRateForShellsAndEqs': 400,
		'exchangeRate': 400,
		'tankmanCost': ({
			'isPremium': False,
			'baseRoleLoss': 0.20000000298023224,
			'gold': 0,
			'credits': 0,
			'classChangeRoleLoss': 0.20000000298023224,
			'roleLevel': 50
		},
		{
			'isPremium': False,
			'baseRoleLoss': 0.10000000149011612,
			'gold': 0,
			'credits': 20000,
			'classChangeRoleLoss': 0.10000000149011612,
			'roleLevel': 75
		},
		{
			'isPremium': True,
			'baseRoleLoss': 0.0,
			'gold': 200,
			'credits': 0,
			'classChangeRoleLoss': 0.0,
			'roleLevel': 100
		}),
		'paidRemovalCost': 10,
		'dailyXPFactor': 2,
		'changeRoleCost': 500,
		'items': getOfflineShopItems(),
		'customization': dict((nation, {'camouflages': {}}) for nation in INDICES.values()),
		'isEnabledBuyingGoldShellsForCredits': True,
		'slotsPrices': (9, [300]),
		'freeXPToTManXPRate': 10,
		'sellPriceFactor': 0.5,
		'isEnabledBuyingGoldEqsForCredits': True,
		'playerInscriptionCost': {
			0: (15, True),
			7: (1500, False),
			30: (6000, False),
			'nations': {}
		}
	}

	baseFunc(baseSelf, syncID, data)

@override(_TimeCorrector, 'serverRegionalTime')
def TimeCorrector_serverRegionalTime(baseFunc, baseSelf):
	regionalSecondsOffset = 0
	try:
		serverRegionalSettings = OFFLINE_SERVER_SETTINGS['regional_settings']
		regionalSecondsOffset = serverRegionalSettings['starting_time_of_a_new_day']
	except Exception:
		LOG_CURRENT_EXCEPTION()
	return _g_instance.serverUTCTime + regionalSecondsOffset

@override(_GameSessionController, 'isSessionStartedThisDay')
def GameSessionController_isSessionStartedThisDay(baseFunc, baseSelf):
	serverRegionalSettings = OFFLINE_SERVER_SETTINGS['regional_settings']
	return int(_g_instance.serverRegionalTime) / 86400 == int(baseSelf._GameSessionController__sessionStartedAt + serverRegionalSettings['starting_time_of_a_new_day']) / 86400

@override(_GameSessionController, '_getWeeklyPlayHours')
def GameSessionController_getWeeklyPlayHours(baseFunc, baseSelf):
	serverRegionalSettings = OFFLINE_SERVER_SETTINGS['regional_settings']
	weekDaysCount = account_shared.currentWeekPlayDaysCount(_g_instance.serverUTCTime, serverRegionalSettings['starting_time_of_a_new_day'], serverRegionalSettings['starting_day_of_a_new_weak'])
	return baseSelf._getDailyPlayHours() + sum(baseSelf._GameSessionController__stats.dailyPlayHours[1:weekDaysCount])

@override(Vehicle, 'canSell')
def Vehicle_canSell(baseFunc, baseSelf):
	return BigWorld.player().isOffline or baseFunc(baseSelf)

@override(Login, 'populateUI')
def Login_populateUI(baseFunc, baseSelf, proxy):
	baseFunc(baseSelf, proxy)
	connectionManager.connect(OFFLINE_SERVER_ADDRESS, OFFLINE_LOGIN, OFFLINE_PWD, False, False, False)

@override(Account.PlayerAccount, '__init__')
def Account_init(baseFunc, baseSelf):
	baseSelf.isOffline = not baseSelf.name
	if baseSelf.isOffline:
		baseSelf.fakeServer = FakeServer()
		baseSelf.name = OFFLINE_NICKNAME
		baseSelf.serverSettings = OFFLINE_SERVER_SETTINGS
		baseSelf._offhangar_arena = _OfflineArenaStub()
		baseSelf._offhangar_vehicle_stub = _OfflineVehicleStub()
		baseSelf._offhangar_allow_world_clear = False
		baseSelf._offline_allow_become_non_player = False
		baseSelf._offhangar_stats501_streak = 0

	baseFunc(baseSelf)

	if baseSelf.isOffline:
		BigWorld.player(baseSelf)

@override(Account.PlayerAccount, '__getattribute__')
def Account_getattribute(baseFunc, baseSelf, name):
	if name == 'vehicle' and baseSelf.isOffline:
		mock = getattr(baseSelf, '_offhangar_mock_veh', None)
		if mock is not None:
			return mock
		return baseFunc(baseSelf, name)
	if name == 'team' and baseSelf.isOffline:
		return getattr(baseSelf, '_offhangar_team', 1)
	if name == 'inputHandler' and baseSelf.isOffline:
		orig_ih = baseFunc(baseSelf, name)
		if orig_ih and not hasattr(orig_ih, 'onCameraChanged'):
			import Event
			orig_ih.onCameraChanged = Event.Event()
			orig_ih.onPostmortemVehicleChanged = Event.Event()
		return orig_ih
	if name in ('arenaTypeID', 'arenaUniqueID') and baseSelf.isOffline:
		try:
			return baseSelf.arena.arenaType.id
		except:
			return 1
	if name == 'setForcedGuiControlMode' and baseSelf.isOffline:
		return lambda *args, **kwargs: None
	if name == 'playerVehicleID' and baseSelf.isOffline:
		ctx = getattr(baseSelf, '_offhangar_battle_ctx', None) or {}
		return ctx.get('playerVehicleID', 0)
	if name == 'vehicleTypeDescriptor' and baseSelf.isOffline:
		try:
			from items import vehicles
			return vehicles.VehicleDescr(typeName='ussr:MS-1')
		except Exception:
			pass
		vehStub = getattr(baseSelf, '_offhangar_vehicle_stub', None)
		if vehStub is None:
			vehStub = _OfflineVehicleStub()
			baseSelf._offhangar_vehicle_stub = vehStub
		td = getattr(vehStub, 'typeDescriptor', None)
		if td is None:
			vehStub.typeDescriptor = _OfflineVehicleStub._TypeDescriptorStub()
			td = vehStub.typeDescriptor
		return td
	if name == 'onGunShotChanged' and baseSelf.isOffline:
		import Event
		if not hasattr(baseSelf, '_offhangar_onGunShotChanged'):
			baseSelf._offhangar_onGunShotChanged = Event.Event()
		return baseSelf._offhangar_onGunShotChanged
	if name == 'playerVehicleID' and baseSelf.isOffline:
		if getattr(baseSelf, '_offhangar_player_vehicle_id', 0):
			return baseSelf._offhangar_player_vehicle_id
		try:
			from CurrentVehicle import g_currentVehicle
			item = getattr(g_currentVehicle, 'item', None)
			if item is not None:
				return getattr(item, 'invID', 0)
		except Exception:
			LOG_CURRENT_EXCEPTION()
		return 0
	if name == 'arena' and baseSelf.isOffline:
		return getattr(baseSelf, '_offhangar_arena', None)
	if name in ('cell', 'base', 'server') and baseSelf.isOffline:
		name = 'fakeServer'
	
	return baseFunc(baseSelf, name)

@override(Account.PlayerAccount, 'onBecomePlayer')
def Account_onBecomePlayer(baseFunc, baseSelf):
	import time
	baseSelf._offline_boot_time = time.time()
	if not hasattr(baseSelf, 'newFakeModel'):
		def newFakeModel():
			import BigWorld
			return BigWorld.Model('')
		baseSelf.newFakeModel = newFakeModel
	baseFunc(baseSelf)
	_ensure_postmortem_event(getattr(baseSelf, 'inputHandler', None))
	if baseSelf.isOffline:
		baseSelf.showGUI(OFFLINE_GUI_CTX)

@override(Account.PlayerAccount, 'handleKeyEvent')
def Account_handleKeyEvent(baseFunc, baseSelf, event):
	import Keys
	if event.isKeyDown() and event.key == Keys.KEY_F12:
		LOG_DEBUG('Offline.F12 pressed -> forcing battle start')
		try:
			from gui.mods.offhangar.offline_battle import start_offline_random_from_hangar
			start_offline_random_from_hangar(baseSelf, 0)
		except Exception:
			LOG_CURRENT_EXCEPTION()
		return True
	return baseFunc(baseSelf, event)

@override(Account.PlayerAccount, 'onBecomeNonPlayer')
def Account_onBecomeNonPlayer(baseFunc, baseSelf):
	import traceback
	LOG_DEBUG('Account.onBecomeNonPlayer() called! Traceback:')
	for line in traceback.format_stack():
		LOG_DEBUG(line.strip())
	if baseSelf.isOffline and not getattr(baseSelf, '_offline_allow_become_non_player', False):
		LOG_DEBUG('OfflineStub.skip onBecomeNonPlayer')
		return
	baseFunc(baseSelf)

@override(BigWorld, 'clearEntitiesAndSpaces')
def BigWorld_clearEntitiesAndSpaces(baseFunc, *args):
	player = BigWorld.player()
	if getattr(player, 'isOffline', False) and not getattr(player, '_offhangar_allow_world_clear', False):
		return
	baseFunc(*args)

@override(BigWorld, 'connect')
def BigWorld_connect(baseFunc, server, loginParams, progressFn):
	if server == OFFLINE_SERVER_ADDRESS:
		LOG_DEBUG('BigWorld.connect')
		progressFn(1, "LOGGED_ON", {})
		BigWorld.createEntity('Account', BigWorld.createSpace(), 0, (0, 0, 0), (0, 0, 0), {})
	else:
		baseFunc(server, loginParams, progressFn)


import game as _game_module
_orig_game_fini = _game_module.fini
def _offline_game_fini():
	player = BigWorld.player()
	if getattr(player, 'isOffline', False) and not getattr(player, '_offline_allow_become_non_player', False):
		LOG_DEBUG('OfflineBattle.blocked game.fini() during battle')
		return
	_orig_game_fini()
_game_module.fini = _offline_game_fini


def _offline_enqueue_random_cmd_id():
	return getattr(AccountCommands, 'CMD_ENQUEUE_RANDOM', 700)


def _install_offline_account__do_cmd_hook():
	if '_PlayerAccount__doCmd' not in dir(Account.PlayerAccount):
		LOG_DEBUG('Offline.__doCmd missing on PlayerAccount')
		return
	try:
		@override(Account.PlayerAccount, '__doCmd')
		def PlayerAccount___doCmd(baseFunc, baseSelf, doCmdMethod, cmd, callback, *args):
			if not getattr(baseSelf, 'isOffline', False):
				return baseFunc(baseSelf, doCmdMethod, cmd, callback, *args)
			if doCmdMethod != 'doCmdInt3' or cmd != _offline_enqueue_random_cmd_id():
				return baseFunc(baseSelf, doCmdMethod, cmd, callback, *args)
			getRid = getattr(baseSelf, '_PlayerAccount__getRequestID', None)
			if not callable(getRid):
				LOG_DEBUG('Offline.__doCmd ENQUEUE_RANDOM skip no __getRequestID')
				return baseFunc(baseSelf, doCmdMethod, cmd, callback, *args)
			rid = getRid()
			if rid is None:
				return baseFunc(baseSelf, doCmdMethod, cmd, callback, *args)
			respMap = getattr(baseSelf, '_PlayerAccount__onCmdResponse', None)
			if callback is not None and respMap is not None:
				respMap[rid] = callback
			vehInvID = args[0] if args else 0

			def _ack_and_boot():
				try:
					import traceback
					LOG_DEBUG('Offline.__doCmd ENQUEUE_RANDOM caller traceback:')
					for line in traceback.format_stack():
						LOG_DEBUG(line.strip())
					baseSelf.onCmdResponse(rid, AccountCommands.RES_SUCCESS, '')
				except Exception:
					LOG_CURRENT_EXCEPTION()
				LOG_DEBUG('Offline.__doCmd ENQUEUE_RANDOM IGNORED')

			LOG_DEBUG('Offline.__doCmd ENQUEUE_RANDOM', rid, vehInvID)
			BigWorld.callback(0.0, _ack_and_boot)
			return rid
	except Exception:
		LOG_CURRENT_EXCEPTION()


def _install_offline_enqueue_public_hooks():
	if hasattr(Account.PlayerAccount, 'enqueueRandom'):
		@override(Account.PlayerAccount, 'enqueueRandom')
		def PlayerAccount_enqueueRandom(baseFunc, baseSelf, *args, **kwargs):
			if getattr(baseSelf, 'isOffline', False):
				LOG_DEBUG('Offline.enqueueRandom IGNORED')
				return
			return baseFunc(baseSelf, *args, **kwargs)
	else:
		LOG_DEBUG('Offline.enqueueRandom missing')

	candidates = []
	for name in dir(Account.PlayerAccount):
		if not callable(getattr(Account.PlayerAccount, name)):
			continue
		low = name.lower()
		if 'tutorial' in low or 'bootcamp' in low or 'sandbox' in low:
			continue
		if 'enqueue' in low and 'random' in low and name != 'enqueueRandom' and not name.startswith('on'):
			candidates.append(name)
	if candidates:
		LOG_DEBUG('Offline.enqueueExtraCandidates', candidates)
	for methodName in candidates:
		try:
			def _bind(nm):
				@override(Account.PlayerAccount, nm)
				def _enqueueAlt(baseFunc, baseSelf, *args, **kwargs):
					if getattr(baseSelf, 'isOffline', False):
						LOG_DEBUG('Offline.intercepted IGNORED', nm, args)
						return
					return baseFunc(baseSelf, *args, **kwargs)
			_bind(methodName)
		except Exception:
			LOG_CURRENT_EXCEPTION()


def _install_offline_battle_transport_hooks():
	_install_offline_account__do_cmd_hook()
	_install_offline_enqueue_public_hooks()


def _install_offline_avatar_guards():
	def _ensure_offline_avatar_state(baseSelf):
		class DummyMProv(object):
			target = None
		defaults = {
			'_PlayerAvatar__stepsTillInit': 1,
			'_PlayerAvatar__isSpaceInitialized': False,
			'_PlayerAvatar__setOwnVehicleMatrixTimerID': 0,
			'_PlayerAvatar__isForcedGuiControlMode': False,
			'_PlayerAvatar__ownVehicleMProv': DummyMProv(),
			'_PlayerAvatar__shotWaitingTimerID': 0,
			'_PlayerAvatar__fireNonFatalDamageTriggerID': 0,
			'playerVehicleID': 0,
		}
		for key, value in defaults.iteritems():
			if not hasattr(baseSelf, key):
				setattr(baseSelf, key, value)
		if not hasattr(baseSelf, 'arena'):
			baseSelf.arena = _OfflineArenaStub()
		if not hasattr(baseSelf, '_offhangar_vehicle_stub'):
			baseSelf._offhangar_vehicle_stub = _OfflineVehicleStub()
		vehStub = baseSelf._offhangar_vehicle_stub
		for attrName in ('_Avatar__vehicleAttached', '_PlayerAvatar__vehicleAttached', '_Avatar__vehicle'):
			if not hasattr(baseSelf, attrName) or getattr(baseSelf, attrName) is None:
				try:
					setattr(baseSelf, attrName, vehStub)
				except TypeError:
					pass
				except Exception:
					LOG_CURRENT_EXCEPTION()

	seen = set()
	for className in ('Avatar', 'PlayerAvatar'):
		avatarCls = getattr(AvatarModule, className, None)
		if avatarCls is None or not hasattr(avatarCls, 'onEnterWorld'):
			continue
		if id(avatarCls) in seen:
			continue
		seen.add(id(avatarCls))

		@override(avatarCls, 'onEnterWorld')
		def _avatar_onEnterWorld(baseFunc, baseSelf, _className=className, *args, **kwargs):
			_ensure_offline_avatar_state(baseSelf)
			try:
				if args:
					return baseFunc(baseSelf, *args, **kwargs)
				return baseFunc(baseSelf, [])
			except KeyError as ex:
				if 'fake_model.model' in str(ex):
					LOG_DEBUG('OfflineAvatar.ignore missing model', _className, ex)
					return
				raise

		if hasattr(avatarCls, 'onLeaveWorld'):
			@override(avatarCls, 'onLeaveWorld')
			def _avatar_onLeaveWorld(baseFunc, baseSelf, _className=className, *args, **kwargs):
				_ensure_offline_avatar_state(baseSelf)
				try:
					return baseFunc(baseSelf, *args, **kwargs)
				except AttributeError as ex:
					msg = str(ex)
					if 'playerVehicleID' in msg or '_PlayerAvatar__stepsTillInit' in msg or '_PlayerAvatar__setOwnVehicleMatrixTimerID' in msg:
						LOG_DEBUG('OfflineAvatar.ignore leave attr', _className, ex)
						return
					raise
				except ValueError as ex:
					msg = str(ex)
					if 'py_cancelCallback' in msg:
						LOG_DEBUG('OfflineAvatar.ignore leave callback', _className, ex)
						return
					raise

		if hasattr(avatarCls, 'getVehicleAttached'):
			@override(avatarCls, 'getVehicleAttached')
			def _avatar_getVehicleAttached(baseFunc, baseSelf, *args, **kwargs):
				try:
					veh = baseFunc(baseSelf, *args, **kwargs)
					if veh is not None:
						return veh
				except Exception:
					LOG_CURRENT_EXCEPTION()
				_ensure_offline_avatar_state(baseSelf)
				return getattr(baseSelf, '_offhangar_vehicle_stub')


def _install_offline_input_guards():
	accIhCls = getattr(Account, 'AccountInputHandler', None)
	if accIhCls is not None and hasattr(accIhCls, '__init__'):
		@override(accIhCls, '__init__')
		def _accIh_init(baseFunc, baseSelf, *args, **kwargs):
			baseFunc(baseSelf, *args, **kwargs)
			_ensure_postmortem_event(baseSelf)
		LOG_DEBUG('OfflineInput.patched AccountInputHandler.__init__')

	aihCls = getattr(AvatarInputHandlerModule, 'AvatarInputHandler', None)
	LOG_DEBUG('OfflineInput._install guards: aihCls:', aihCls)
	if aihCls is not None:
		LOG_DEBUG('OfflineInput._install guards: hasattr start:', hasattr(aihCls, 'start'))
	if aihCls is None or not hasattr(aihCls, 'start'):
		LOG_DEBUG('OfflineInput._install guards: EARLY RETURN!')
		return

	if hasattr(aihCls, '__init__'):
		@override(aihCls, '__init__')
		def _aih_init(baseFunc, baseSelf, *args, **kwargs):
			baseFunc(baseSelf, *args, **kwargs)
			_ensure_postmortem_event(baseSelf)
		LOG_DEBUG('OfflineInput.patched AvatarInputHandler.__init__')


install_game_session_guards()
_install_offline_battle_transport_hooks()
_install_offline_avatar_guards()
_install_offline_input_guards()
