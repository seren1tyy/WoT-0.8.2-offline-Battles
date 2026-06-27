from GameSessionController import _GameSessionController

from gui.mods.offhangar.logging import LOG_DEBUG
from gui.mods.offhangar.utils import override


FORCE_ALLOW_BATTLE_ENTRY = False


def normalize_offline_stats(stats):
	"""Disable anti-offline restrictions in account stats payload."""
	if not isinstance(stats, dict):
		return
	stats['battlesTillCaptcha'] = 0
	stats['captchaTriesLeft'] = 0
	stats['restrictions'] = {}


def _always_false(baseFunc, baseSelf, *args, **kwargs):
	return False

def _always_zero(baseFunc, baseSelf, *args, **kwargs):
	return 0



def _always_24h(baseFunc, baseSelf, *args, **kwargs):
	return 24 * 3600


def _always_true(baseFunc, baseSelf, *args, **kwargs):
	return True


def install_game_session_guards():
	"""
	Patch GameSessionController restrictions that can block battle entry.
	Only patch existing methods to stay compatible with different 0.8.x builds.
	"""
	patches_false = (
		'needCaptcha',
		'isCaptchaRequired',
		'isParentControlActive',
		'isParentControlEnabled',
		'hasActiveSessionLimit',
		'isSessionStartedThisDay'
	)
	for method_name in patches_false:
		if hasattr(_GameSessionController, method_name):
			override(_GameSessionController, method_name)(_always_false)
			LOG_DEBUG('SessionGuard.disable', method_name)

	
	for method_name in ('getDailyPlayTimeLeft', 'getWeeklyPlayTimeLeft'):
		if hasattr(_GameSessionController, method_name):
			override(_GameSessionController, method_name)(_always_24h)
			LOG_DEBUG('SessionGuard.disable', method_name)

	for method_name in ('_getDailyPlayHours', '_getWeeklyPlayHours'):
		if hasattr(_GameSessionController, method_name):
			override(_GameSessionController, method_name)(_always_zero)
			LOG_DEBUG('SessionGuard.disable', method_name)

	if FORCE_ALLOW_BATTLE_ENTRY:
		for method_name in ('isAccountAllowedToBattle', 'canJoinBattle'):
			if hasattr(_GameSessionController, method_name):
				override(_GameSessionController, method_name)(_always_true)
				LOG_DEBUG('SessionGuard.forceAllowBattle', method_name)




def install_dossier_guard():
    try:
        import dossiers
        from gui.mods.offhangar.logging import LOG_DEBUG
        _orig = dossiers.getAccountDossierDescr
        def _safe(compDescr):
            if compDescr is None:
                compDescr = ''
            return _orig(compDescr)
        dossiers.getAccountDossierDescr = _safe
        LOG_DEBUG('SessionGuard.dossier_guard_installed')
    except Exception:
        from debug_utils import LOG_CURRENT_EXCEPTION
        LOG_CURRENT_EXCEPTION()

install_dossier_guard()


def install_sync_data_guard():
    """
    Patch AccountSyncData.waitForSync so that in offline mode it immediately
    fires callbacks instead of waiting. This makes Stats.get() return cached
    values right away rather than returning None while waiting for a server.
    """
    try:
        from account_helpers.AccountSyncData import AccountSyncData
        from AccountCommands import RES_CACHE
        from gui.mods.offhangar.logging import LOG_DEBUG
        _orig_waitForSync = AccountSyncData.waitForSync
        def _offline_waitForSync(self, callback):
            if self._AccountSyncData__isSynchronized:
                if callback is not None:
                    callback(RES_CACHE)
                return
            if not self._AccountSyncData__ignore:
                # Force synchronized so future calls fire immediately
                self._AccountSyncData__isSynchronized = True
                if callback is not None:
                    callback(RES_CACHE)
                return
            return _orig_waitForSync(self, callback)
        AccountSyncData.waitForSync = _offline_waitForSync
        LOG_DEBUG('SessionGuard.sync_data_guard_installed')
    except Exception:
        from debug_utils import LOG_CURRENT_EXCEPTION
        LOG_CURRENT_EXCEPTION()

install_sync_data_guard()


def install_stats_defaults_guard():
    """
    Patch Stats.__onGetResponse to inject safe defaults for any stat that is
    still None after the cache lookup. This handles the race between Hangar
    requesting stats and Stats.synchronize() actually running.
    """
    try:
        from account_helpers.Stats import Stats
        from gui.mods.offhangar.logging import LOG_DEBUG
        _DEFAULTS = {
            'eliteVehicles': set(),
            'multipliedXPVehs': set(),
            'unlocks': set(),
            'vehTypeXP': {},
            'vehTypeLocks': {},
            'globalVehicleLocks': {},
            'restrictions': {},
            'slots': 2000,
            'dossier': '',
            'credits': 100000000,
            'gold': 1000000,
            'berths': 40,
            'freeXP': 100000000,
            'clanInfo': ('', '', 0, 0, 0),
            'accOnline': 0,
            'accOffline': 0,
            'freeTMenLeft': 0,
            'freeVehiclesLeft': 0,
            'vehicleSellsLeft': 0,
            'captchaTriesLeft': 0,
            'hasFinPassword': True,
            'finPswdAttemptsLeft': 0,
            'tkillIsSuspected': False,
            'denunciationsLeft': 0,
            'tutorialsCompleted': 33553532,
            'battlesTillCaptcha': 99,
            'dailyPlayHours': [0],
            'playLimits': ((0, ''), (0, '')),
            'clanDBID': 0,
            'attrs': 0,
            'premiumExpiryTime': 0,
            'autoBanTime': 0,
            'oldVehInvID': 0,
            'globalRating': 0,
            'fortResource': 0,
            'unitAcceptDeadline': 0,
        }
        _orig = Stats._Stats__onGetResponse
        def _safe_onGetResponse(self, statName, callback, resultID):
            # We must intercept the inner callback instead, because __onGetResponse
            # looks up the value in cache and passes it to the callback!
            def _inner_callback(resID, val, *args, **kwargs):
                if val is None and statName in _DEFAULTS:
                    val = _DEFAULTS[statName]
                if callback:
                    callback(resID, val, *args, **kwargs)
            return _orig(self, statName, _inner_callback if callback else None, resultID)
        Stats._Stats__onGetResponse = _safe_onGetResponse
        LOG_DEBUG('SessionGuard.stats_defaults_guard_installed')
    except Exception:
        from debug_utils import LOG_CURRENT_EXCEPTION
        LOG_CURRENT_EXCEPTION()

install_stats_defaults_guard()


def install_customization_guard():
    try:
        from gui.Scaleform.customization.BaseTimedCustomizationInterface import BaseTimedCustomizationInterface
        from gui.mods.offhangar.logging import LOG_DEBUG
        _orig = BaseTimedCustomizationInterface.getItemCost
        def _safe_getItemCost(self, itemId, priceIndex):
            try:
                return _orig(self, itemId, priceIndex)
            except AttributeError:
                # Occurs if packageCost is None due to offline shop data missing packages
                return {'cost': 0, 'isGold': False}
        BaseTimedCustomizationInterface.getItemCost = _safe_getItemCost
        _origHandle = BaseTimedCustomizationInterface._BaseTimedCustomizationInterface__handleRentalPackageChange
        def _safe_handleRentalPackageChange(self, *args, **kwargs):
            if getattr(self, '_rentalPackageDP', None) is not None:
                item = getattr(self._rentalPackageDP, 'selectedPackage', None)
                if item is None or item.get('cost', -1) <= -1:
                    if hasattr(self, '_itemsDP'):
                        self._itemsDP.setDefaultCost(0, False)
                        self._itemsDP.refresh()
                    return
            return _origHandle(self, *args, **kwargs)
        BaseTimedCustomizationInterface._BaseTimedCustomizationInterface__handleRentalPackageChange = _safe_handleRentalPackageChange
        
        LOG_DEBUG('SessionGuard.customization_guard_installed')
    except Exception:
        pass

install_customization_guard()


def log_credits():
    try:
        from gui.mods.offhangar.logging import LOG_DEBUG
        import BigWorld
        if hasattr(BigWorld.player(), 'stats'):
            def cb(res, val=None):
                LOG_DEBUG('Credits test:', res, val)
            BigWorld.player().stats.get('credits', cb)
    except Exception as e:
        pass

import BigWorld
if hasattr(BigWorld, 'callback'):
    BigWorld.callback(5.0, log_credits)


def install_all_sync_guards():
    try:
        from account_helpers.Shop import Shop
        from account_helpers.Inventory import Inventory
        from account_helpers.DossierCache import DossierCache
        from account_helpers.Stats import Stats
        from account_helpers.AccountSyncData import AccountSyncData
        from account_helpers.AccountSettings import AccountSettings
        from account_helpers.Trader import Trader
        from AccountCommands import RES_CACHE

        classes = [Shop, Inventory, DossierCache, Stats, AccountSyncData, Trader, AccountSettings]
        for cls in classes:
            if not hasattr(cls, 'waitForSync'): continue
            _orig = cls.waitForSync
            def make_wrapper(orig_method, class_name):
                def _offline_waitForSync(self, callback):
                    if hasattr(self, '_' + class_name + '__ignore') and getattr(self, '_' + class_name + '__ignore'):
                        if callback: callback(-1)
                        return
                    if hasattr(self, '_' + class_name + '__isSynchronizing') and getattr(self, '_' + class_name + '__isSynchronizing'):
                        setattr(self, '_' + class_name + '__isSynchronizing', False)
                    # Force call immediately
                    if callback:
                        try:
                            rev = getattr(self, '_' + class_name + '__getCacheRevision')()
                            callback(RES_CACHE, rev)
                        except Exception:
                            try: callback(RES_CACHE)
                            except Exception: pass
                    return
                return _offline_waitForSync
            cls.waitForSync = make_wrapper(_orig, cls.__name__)
    except Exception:
        pass

install_all_sync_guards()

install_game_session_guards()

def patch_inventory_setAndFillLayouts():
    try:
        import AccountCommands
        from account_helpers.Inventory import Inventory
        from gui.mods.offhangar.logging import LOG_DEBUG
        _orig = Inventory._Inventory__setAndFillLayouts_onShopSynced
        def _patched(self, vehInvID, shellsLayout, eqsLayout, callback, resultID, shopRev):
            LOG_DEBUG('Inventory.__setAndFillLayouts_onShopSynced called', vehInvID, shellsLayout, eqsLayout, resultID, shopRev)
            return _orig(self, vehInvID, shellsLayout, eqsLayout, callback, resultID, shopRev)
        Inventory._Inventory__setAndFillLayouts_onShopSynced = _patched
    except Exception:
        pass
patch_inventory_setAndFillLayouts()


def patch_check_credits_requester():
    try:
        from gui.mods.offhangar.logging import LOG_DEBUG
        from gui.Scaleform.utils.requesters import StatsRequester
        def cb(credits):
            LOG_DEBUG('Requester Credits from patch:', credits)
        StatsRequester().getCredits(cb)
    except Exception as e:
        from debug_utils import LOG_CURRENT_EXCEPTION
        LOG_CURRENT_EXCEPTION()

def log_requester_on_login():
    import BigWorld
    import Account
    _orig = Account.PlayerAccount.onBecomePlayer
    def _patched(self, *args, **kwargs):
        _orig(self, *args, **kwargs)
        BigWorld.callback(2.0, patch_check_credits_requester)
    Account.PlayerAccount.onBecomePlayer = _patched
log_requester_on_login()

def patch_technical_maintenance():
    from gui.Scaleform.TechnicalMaintenance import TechnicalMaintenance
    _orig_populateUI = TechnicalMaintenance.populateUI
    def _patched_populateUI(self, *args, **kwargs):
        _orig_populateUI(self, *args, **kwargs)
        try:
            if hasattr(self, 'flashObject') and self.flashObject is not None:
                if hasattr(self.flashObject, 'as_setCredits'):
                    self.flashObject.as_setCredits(100000000)
                if hasattr(self.flashObject, 'as_setGold'):
                    self.flashObject.as_setGold(1000000)
        except Exception as e:
            pass
    TechnicalMaintenance.populateUI = _patched_populateUI

patch_technical_maintenance()

def patch_technical_maintenance_credits():
    from gui.Scaleform.TechnicalMaintenance import TechnicalMaintenance
    _orig_populateUI = TechnicalMaintenance.populateUI
    def _patched_populateUI(self, *args, **kwargs):
        _orig_populateUI(self, *args, **kwargs)
        try:
            if hasattr(self, 'uiHolder') and hasattr(self.uiHolder, 'call'):
                self.uiHolder.call('techMaintenance.setCredits', [100000000])
                self.uiHolder.call('techMaintenance.setGold', [1000000])
        except Exception as e:
            from debug_utils import LOG_CURRENT_EXCEPTION
            LOG_CURRENT_EXCEPTION()
    TechnicalMaintenance.populateUI = _patched_populateUI

patch_technical_maintenance_credits()

def patch_tech_maintenance_update():
    from gui.Scaleform.TechnicalMaintenance import TechnicalMaintenance
    from gui.ClientUpdateManager import g_clientUpdateManager
    _orig_populateUI = TechnicalMaintenance.populateUI
    def _patched_populateUI(self, *args, **kwargs):
        _orig_populateUI(self, *args, **kwargs)
        try:
            import BigWorld
            if hasattr(BigWorld.player(), 'stats'):
                BigWorld.player().stats.get('credits', lambda resID, val: g_clientUpdateManager.update({'stats': {'credits': val}}))
                BigWorld.player().stats.get('gold', lambda resID, val: g_clientUpdateManager.update({'stats': {'gold': val}}))
            else:
                g_clientUpdateManager.update({'stats': {'credits': 100000000, 'gold': 1000000}})
        except Exception as e:
            from debug_utils import LOG_CURRENT_EXCEPTION
            LOG_CURRENT_EXCEPTION()
    TechnicalMaintenance.populateUI = _patched_populateUI

patch_tech_maintenance_update()

def test_stats_credits():
    from gui.Scaleform.TechnicalMaintenance import TechnicalMaintenance
    _orig_populateUI = TechnicalMaintenance.populateUI
    def _patched_populateUI(self, *args, **kwargs):
        _orig_populateUI(self, *args, **kwargs)
        import BigWorld
        from debug_utils import LOG_DEBUG
        BigWorld.player().stats.get('credits', lambda resID, val: LOG_DEBUG('TEST CREDITS:', val))
        try:
            self.uiHolder.call('techMaintenance.setCredits', [100000000])
        except: pass
    TechnicalMaintenance.populateUI = _patched_populateUI
test_stats_credits()

def patch_tech_maintenance_force_credits():
    from gui.Scaleform.TechnicalMaintenance import TechnicalMaintenance
    _orig_populateUI = TechnicalMaintenance.populateUI
    def _patched_populateUI(self, *args, **kwargs):
        _orig_populateUI(self, *args, **kwargs)
        try:
            self.uiHolder.call('common.creditsResponse', [100000000])
            self.uiHolder.call('common.goldResponse', [1000000])
        except: pass
    TechnicalMaintenance.populateUI = _patched_populateUI

patch_tech_maintenance_force_credits()

def patch_tech_maintenance_log_price():
    from gui.Scaleform.TechnicalMaintenance import TechnicalMaintenance
    _orig_populate = TechnicalMaintenance.onPopulateTechnicalMaintenance
    def _patched_populate(self, *args, **kwargs):
        from debug_utils import LOG_DEBUG
        import BigWorld
        LOG_DEBUG('---- STATS GET CREDITS ----', BigWorld.player().stats.get('credits'))
        res = _orig_populate(self, *args, **kwargs)
        return res
    TechnicalMaintenance.onPopulateTechnicalMaintenance = _patched_populate

patch_tech_maintenance_log_price()

def loop_credits_update():
    from gui.ClientUpdateManager import g_clientUpdateManager
    import BigWorld
    def _loop():
        try:
            g_clientUpdateManager.update({'stats': {'credits': 100000000, 'gold': 1000000}})
        except:
            pass
        BigWorld.callback(1.0, _loop)
    _loop()
import BigWorld
BigWorld.callback(5.0, loop_credits_update)

def patch_shop_requester():
    from gui.Scaleform.utils.requesters import ShopRequester
    ShopRequester.isEnabledBuyingGoldShellsForCredits = property(lambda self: True)
    ShopRequester.isEnabledBuyingGoldEqsForCredits = property(lambda self: True)

patch_shop_requester()

def print_stats_cache():
    import BigWorld
    def _cb(*args):
        from debug_utils import LOG_DEBUG
        LOG_DEBUG('---- CACHE KEYS ----', BigWorld.player().stats._Stats__cache.keys())
        LOG_DEBUG('---- CACHE CREDITS ----', BigWorld.player().stats._Stats__cache.get('credits'))
    BigWorld.player().stats.getCache(_cb)
import BigWorld
BigWorld.callback(3.0, print_stats_cache)

def patch_stats_requesterr_hardcode():
    try:
        from gui.Scaleform.utils.requesters import StatsRequesterr
        StatsRequesterr.credits = property(lambda self: 100000000)
        StatsRequesterr.gold = property(lambda self: 1000000)
    except: pass

patch_stats_requesterr_hardcode()

def patch_resolve_selected_compact_descr():
    try:
        from gui.mods.offhangar import offline_battle_stack
        _orig = offline_battle_stack._resolve_selected_compact_descr
        def _patched(player):
            res = _orig(player)
            if not res:
                from items import vehicles
                nationID = 0
                vehID = vehicles.g_list.getList(nationID).keys()[0]
                res = vehicles.makeIntCompactDescrByID('vehicle', nationID, vehID)
            return res
        offline_battle_stack._resolve_selected_compact_descr = _patched
    except: pass
patch_resolve_selected_compact_descr()

def fix_offline_battle_crashes():
    try:
        from gui.Scaleform.BattleDispatcherInterface import BattleDispatcherInterface
        _orig_onFightButtonClick = BattleDispatcherInterface.onFightButtonClick
        def _patched_onFightButtonClick(self, actionName, prebattleID='', *args, **kwargs):
            from debug_utils import LOG_DEBUG
            try:
                import BigWorld
                from gui.mods.mod_offhangar import start_offline_random_from_hangar
                from CurrentVehicle import g_currentVehicle
                invID = g_currentVehicle.vehicle.inventoryId if g_currentVehicle.isPresent() else 1
                LOG_DEBUG('OfflineBattle.bypass enqueue random')
                start_offline_random_from_hangar(BigWorld.player(), invID)
                return None
            except Exception as e:
                LOG_DEBUG('OfflineBattle.bypass exception', e)
            return _orig_onFightButtonClick(self, actionName, prebattleID, *args, **kwargs)
        BattleDispatcherInterface.onFightButtonClick = _patched_onFightButtonClick
    except Exception as e:
        pass

    try:
        from gui import BattleContext
        _orig_addVehicleData = BattleContext._BattleContext__addVehicleData
        def _patched_addVehicleData(self, vID, vData):
            vDesc = vData.get('vehicleType')
            if isinstance(vDesc, (int, long)):
                class FakeType:
                    tags = frozenset()
                class FakeDesc:
                    type = FakeType()
                vData['vehicleType'] = FakeDesc()
            return _orig_addVehicleData(self, vID, vData)
        BattleContext._BattleContext__addVehicleData = _patched_addVehicleData
    except Exception as e:
        pass

fix_offline_battle_crashes()

def patch_captcha_view():
    try:
        from gui.Scaleform.CaptchaView import CaptchaView
        
        def _mock_showCaptcha(*args, **kwargs):
            for a in args:
                if callable(a): a()
            for v in kwargs.values():
                if callable(v): v()
        CaptchaView.showCaptcha = _mock_showCaptcha
    except Exception:
        pass
patch_captcha_view()

def patch_stats_requesterr_captcha():
    try:
        from gui.Scaleform.utils.requesters import StatsRequesterr
        def _mock_btc(*args, **kwargs):
            for a in args:
                if callable(a): a(99)
            for k, v in kwargs.items():
                if callable(v): v(99)
            return 99
        StatsRequesterr.battlesTillCaptcha = _mock_btc
    except: pass
patch_stats_requesterr_captcha()
