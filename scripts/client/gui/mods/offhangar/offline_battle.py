"""
Offline battle bootstrap for FakeServer.

Real servers answer enqueue with BW callbacks (onEnqueued / onArenaCreated). With only
FakeServer.doCmd* responses the client can exit or hang, so we replay the minimal chain.
Compatible checks keep this safe across 0.8.x builds that differ slightly.
"""

import time
import cPickle
from debug_utils import LOG_DEBUG, LOG_CURRENT_EXCEPTION

g_offline_models = []
def _add_model(m):
	global g_offline_models
	g_offline_models.append(m)
	import BigWorld
	BigWorld.addModel(m)

import BigWorld
try:
	from projectilemover import ProjectileMover
	g_projectile_mover = ProjectileMover()
except Exception as e:
	LOG_DEBUG('Could not init ProjectileMover:', e)
	g_projectile_mover = None

from gui.mods.offhangar.logging import LOG_DEBUG
from gui.mods.offhangar.offline_battle_stack import build_offline_battle_context

_BATTLE_BOOT_DEBOUNCE_SEC = 1.5
OFFLINE_BATTLE_ENABLED = True


def _resolve_real_arena_type(map_id, map_name, gameplay_name):
	"""
	Try to resolve a real ArenaType object from the client's cache.
	This provides minimap + other per-map metadata needed by battle GUI.
	"""
	try:
		try:
			import ArenaType as ArenaTypeModule
		except ImportError:
			# 0.8.2 ships it as `common/arenatype.pyc`
			try:
				from common import arenatype as ArenaTypeModule
			except ImportError:
				import arenatype as ArenaTypeModule
		cache = getattr(ArenaTypeModule, 'g_cache', None)
		# Lazy init on some builds: cache can start as None.
		for init_name in ('init', '_init', 'initialize'):
			init_fn = getattr(ArenaTypeModule, init_name, None)
			if callable(init_fn):
				try:
					init_fn()
					cache = getattr(ArenaTypeModule, 'g_cache', None)
				except Exception:
					LOG_CURRENT_EXCEPTION()
			if cache is not None:
				break

		if cache is None:
			LOG_DEBUG('OfflineBattle.arenaType.cacheMissing', map_name, 'module', getattr(ArenaTypeModule, '__name__', '?'))
			return None

		# Some builds provide module-level getters instead of direct cache access.
		for fn_name in ('getArenaType', 'getByGeometryName', 'getByName', 'getArenaTypeByName'):
			fn = getattr(ArenaTypeModule, fn_name, None)
			if callable(fn):
				for key in (map_name, map_id):
					try:
						at = fn(key)
						if at is not None:
							try:
								at.geometryName = map_name
								at.gameplayName = gameplay_name
							except Exception:
								pass
							return at
					except Exception:
						continue

		def _try_get(key):
			for getter in (
				lambda: cache.get(key),
				lambda: cache[key],
				lambda: cache.getArenaType(key) if hasattr(cache, 'getArenaType') else None,
				lambda: cache.getByID(key) if hasattr(cache, 'getByID') else None,
				lambda: cache.getById(key) if hasattr(cache, 'getById') else None,
			):
				try:
					at = getter()
					if at is not None:
						return at
				except Exception:
					continue
			return None

		# g_cache can be a mapping-like object; try the common access patterns.
		at = _try_get(map_name)
		if at is None and map_id:
			at = _try_get(map_id)
		# If stack provided short name like "himmelsdorf", try to match "04_himmelsdorf".
		if at is None and map_name and '_' not in map_name:
			try:
				keys = cache.keys() if hasattr(cache, 'keys') else []
				for k in keys:
					try:
						if isinstance(k, basestring) and (k == map_name or k.endswith('_' + map_name)):
							at = _try_get(k)
							if at is not None:
								map_name = k
								break
					except Exception:
						continue
			except Exception:
				LOG_CURRENT_EXCEPTION()
		if at is not None:
			try:
				at.geometryName = map_name
				at.gameplayName = gameplay_name
			except Exception:
				pass
			return at

		# 0.8.2: g_cache can be a dict keyed by arenaTypeID (int), with geometryName stored on values.
		try:
			if isinstance(cache, dict):
				for k, v in cache.iteritems():
					try:
						geom = getattr(v, 'geometryName', None) or ''
						if not isinstance(geom, basestring):
							continue
						if geom == map_name or geom.endswith('_' + map_name) or map_name.endswith('_' + geom):
							try:
								v.gameplayName = gameplay_name
							except Exception:
								pass
							return v
					except Exception:
						continue
		except Exception:
			LOG_CURRENT_EXCEPTION()

		# Diagnostics: log cache shape so we can implement the correct lookup for 0.8.2.
		try:
			cache_type = type(cache).__name__
			attrs = [a for a in dir(cache) if 'get' in a.lower() or 'arena' in a.lower() or 'type' in a.lower()]
			if isinstance(cache, dict):
				keys = cache.keys()
				key_types = {}
				for kk in keys[:50]:
					kt = type(kk).__name__
					key_types[kt] = key_types.get(kt, 0) + 1
				# also sample a few geometry names to confirm value shape
				sample_geom = []
				for vv in cache.values()[:10]:
					try:
						g = getattr(vv, 'geometryName', None)
						if g:
							sample_geom.append(g)
					except Exception:
						continue
				LOG_DEBUG(
					'OfflineBattle.arenaType.cacheNoHit',
					map_name, 'mapID', map_id,
					'cacheType', cache_type,
					'keyTypes', key_types,
					'sampleGeom', sample_geom[:5],
					'attrs', attrs[:20]
				)
			else:
				LOG_DEBUG('OfflineBattle.arenaType.cacheNoHit', map_name, 'mapID', map_id, 'cacheType', cache_type, 'attrs', attrs[:25])
		except Exception:
			LOG_CURRENT_EXCEPTION()
	except Exception:
		LOG_CURRENT_EXCEPTION()
	return None


def _queue_type_randoms():
	try:
		from constants import QUEUE_TYPE
		return QUEUE_TYPE.RANDOMS
	except Exception:
		# Very old builds: keep a sane default; onEnqueued may still accept an int.
		return 1


def _resolve_vehicle_inv_id(player, int1):
	if int1:
		return int1
	try:
		from CurrentVehicle import g_currentVehicle
		if g_currentVehicle is not None:
			item = getattr(g_currentVehicle, 'item', None)
			if item is not None:
				vid = getattr(item, 'invID', None)
				if vid:
					return vid
	except ImportError:
		pass
	except Exception:
		LOG_CURRENT_EXCEPTION()
	inv = getattr(player, 'inventory', None)
	if inv is None:
		return 0
	for methodName in (
		'getCurrVehicleInvID',
		'getCurrentVehInvID',
		'getVehicleInvID',
		'getCurrentInvID',
	):
		fn = getattr(inv, methodName, None)
		if callable(fn):
			try:
				v = fn()
				if v:
					return v
			except Exception:
				LOG_CURRENT_EXCEPTION()
	for methodName in ('getCurrentVehicle', 'getCurrVehicle'):
		fn = getattr(inv, methodName, None)
		if callable(fn):
			try:
				veh = fn()
				if veh is not None:
					vid = getattr(veh, 'invID', None)
					if vid:
						return vid
			except Exception:
				LOG_CURRENT_EXCEPTION()
	return 0


def _enable_offline_battle_transition(player):
	# Hangar hardening hooks in mod_offhangar must relax while loading an arena.
	player._offhangar_allow_world_clear = True
	# Allow become-non-player only after avatar spawn attempt.
	player._offline_allow_become_non_player = False


def _try_spawn_battle_avatar_stub(player, cmdName):
	import BigWorld
	if player is None or not getattr(player, 'isOffline', False):
		return
	try:
		space_id = getattr(player, 'spaceID', 0)
		if space_id == 0:
			space_id = BigWorld.createSpace()
		else:
			BigWorld.clearSpace(space_id)
		
		map_name = player.arena.arenaType.geometryName
		if not map_name.startswith('spaces/'):
			map_name = 'spaces/' + map_name
		BigWorld.addSpaceGeometryMapping(space_id, None, map_name)
		LOG_DEBUG('OfflineBattle.mappedGeometry', map_name, 'space', space_id)
	except Exception:
		LOG_CURRENT_EXCEPTION()

	try:
		LOG_DEBUG('OfflineBattle.starting camera manually in space', space_id)
		import AvatarInputHandler
		import Math, ResMgr
		global g_offline_aih

		# Determine spawn position from arena XML
		spawn_pos = Math.Vector3(0, 100.0, 0)
		spawn_dir = Math.Vector3(0, 0, 3.1415926535)
		try:
			at = player.arena.arenaType
			settings_path = 'spaces/%s/space.settings' % at.geometryName.split('/')[-1]
			space_settings = ResMgr.openSection(settings_path)
			LOG_DEBUG('OfflineBattle.SPACE_LOAD:', settings_path, space_settings is not None)
			if space_settings is not None:
				try:
					if space_settings.has_key('startPosition'):
						spawn_pos = space_settings.readVector3('startPosition')
					if space_settings.has_key('startDirection'):
						spawn_dir = space_settings.readVector3('startDirection')
					LOG_DEBUG('OfflineBattle.spawn space.settings:', spawn_pos, spawn_dir)
				except Exception:
					pass

			if space_settings is None or (spawn_pos.x == 0.0 and spawn_pos.y == 0.0 and spawn_pos.z == 0.0) or spawn_pos.y == 100.0:
				xml_path = 'scripts/arena_defs/%s.xml' % at.geometryName.split('/')[-1]
				section = ResMgr.openSection(xml_path)
				LOG_DEBUG('OfflineBattle.XML_LOAD:', xml_path, section is not None)
				if section is not None:
					gp = section['gameplayTypes/ctf']
					if gp is not None:
						sp = gp['teamSpawnPoints/team1']
						bp = gp['teamBasePositions/team1']
						if sp is not None and len(sp.keys()) > 0:
							for key, val in sp.items():
								if 'position' in key:
									vec2 = val.asVector2
									# Find terrain height at this x, z!
									y = 100.0
									try:
										import BigWorld
										# Cast a ray from sky to ground
										hit = BigWorld.wg_collideSegment(player.spaceID, Math.Vector3(vec2.x, 1000.0, vec2.y), Math.Vector3(vec2.x, -1000.0, vec2.y), 128)
										if hit is not None:
											y = hit[0].y
									except: pass
									spawn_pos = Math.Vector3(vec2.x, y, vec2.y)
									LOG_DEBUG('OfflineBattle.spawn pos:', spawn_pos)
									break
						elif bp is not None:
							for key, val in bp.items():
								if 'position' in key or key.isdigit():
									vec2 = val.asVector2
									y = 100.0
									try:
										import BigWorld
										hit = BigWorld.wg_collideSegment(player.spaceID, Math.Vector3(vec2.x, 1000.0, vec2.y), Math.Vector3(vec2.x, -1000.0, vec2.y), 128)
										if hit is not None:
											y = hit[0].y
									except: pass
									spawn_pos = Math.Vector3(vec2.x, y, vec2.y)
									LOG_DEBUG('OfflineBattle.spawn bp pos:', spawn_pos)
									break
		except Exception as e:
			LOG_DEBUG('OfflineBattle.XML_ERROR:', str(e))

		# Use a MatrixProduct as the live vehicle matrix provider.
		# Math.Matrix is a STATIC snapshot - WGTranslationOnlyMP.source needs a C++ live provider.
		# MatrixProduct(a=identity, b=identity) acts as a live provider and can be .set()-like via its parts.
		veh_matrix_static = Math.Matrix()
		veh_matrix_static.setTranslate(spawn_pos)
		veh_matrix = Math.MatrixProduct()
		veh_matrix.a = veh_matrix_static
		veh_matrix.b = Math.Matrix()  # identity
		
		# Chassis matrix: includes yaw + position, driven by Servo
		# so hull/turret/gun chain stays perfectly in sync
		chassis_m = Math.Matrix()
		chassis_m.setTranslate(spawn_pos)
		chassis_mp = Math.MatrixProduct()
		chassis_mp.a = chassis_m
		chassis_mp.b = Math.Matrix()  # identity

		class _MockFilter(object): pass
		mf = _MockFilter()
		mf.position = Math.Vector3(spawn_pos)
		mf.yaw = 0.0
		mf.pitch = 0.0
		mf.matrix = veh_matrix

		class _Appearance(object):
			def changeVisibility(self, part, visible, lod=True): pass
			def showStickers(self, visible): pass
			def isUnderwater(self): return False
			def __getattr__(self, name):
				if 'turretMatrix' in name:
					if self.compoundModel is not None:
						try:
							return self.compoundModel.node('HP_turretJoint')
						except Exception:
							pass
					return turret_matrix
				if 'gunMatrix' in name:
					if self.compoundModel is not None:
						try:
							return self.compoundModel.node('HP_gunJoint')
						except Exception:
							pass
					return turret_matrix
				if 'hullMatrix' in name:
					if self.compoundModel is not None:
						try:
							return self.compoundModel.node('V')
						except Exception:
							pass
					return turret_matrix
				if 'Matrix' in name or 'Prov' in name:
					if self.compoundModel is not None:
						return self.compoundModel.matrix
					return turret_matrix
				if 'Bounds' in name:
					import Math
					return (Math.Vector3(-1,-1,-1), Math.Vector3(1,1,1))
				if name.startswith(('is','on','set','get','update','show','hide','add','remove','play','stop','start')) or name == 'refresh':
					return lambda *a, **k: None
				import Math
				return Math.Matrix()

		ma = _Appearance()

		td = None
		try:
			if hasattr(player, '_offhangar_battle_ctx'):
				ctx = player._offhangar_battle_ctx
				vdict = ctx.get('vehicles', {})
				vid = player.playerVehicleID
				vinfo = vdict.get(vid)
				if not vinfo and vdict:
					vinfo = list(vdict.values())[0]
				if vinfo:
					td = vinfo.get('vehicleType')
			
			from items import vehicles
			if type(td) is int:
				nationID = (td >> 4) & 15
				vehicleID = td >> 8
				td = vehicles.VehicleDescr(typeID=(nationID, vehicleID))
				LOG_DEBUG('PHYSICS_DUMP:', td.physics)
			elif td is None:
				td = vehicles.VehicleDescr(typeName='ussr:MS-1')
			elif type(td).__name__ == 'FakeDesc':
				# If offline_battle_stack gave us FakeDesc, fallback to MS-1 so we don't crash
				td = vehicles.VehicleDescr(typeName='ussr:MS-1')
		except Exception as e:
			LOG_DEBUG('OfflineBattle.td error', str(e))

		LOG_DEBUG('OfflineBattle.td resolved:', td, type(td).__name__ if td else None)
		if td is not None:
			LOG_DEBUG('OfflineBattle.td types:', type(td.chassis), type(td.hull), type(td.turret))
			if hasattr(td.chassis, 'keys'):
				LOG_DEBUG('OfflineBattle.td keys:', td.chassis.keys())

		# Inject into player so the GUI finds it!
		player.vehicleTypeDescriptor = td

		loaded_models = {'chassis': None, 'hull': None, 'turret': None, 'gun': None, 'td': td}
		if td is not None:
			for part_name in ('chassis', 'hull', 'turret', 'gun'):
				try:
					part_desc = getattr(td, part_name, None)
					if part_desc is not None and 'models' in part_desc and 'undamaged' in part_desc['models']:
						modelName = part_desc['models']['undamaged']
						m = BigWorld.Model(modelName)
						loaded_models[part_name] = m
						LOG_DEBUG('OfflineBattle.model loaded:', part_name, modelName)
				except Exception as e:
					LOG_DEBUG('OfflineBattle load model error:', part_name, str(e))
			
			# BigWorld.Model() is async - the model isn't ready immediately.
			# Use a callback to add them after they've loaded.
			_models_to_add = dict((k, v) for k, v in loaded_models.items() if v is not None)
			_add_attempts = [0]
			
			
			def _add_models_when_ready():
				_add_attempts[0] += 1
				try:
					chassis = _models_to_add.get('chassis')
					hull    = _models_to_add.get('hull')
					turret  = _models_to_add.get('turret')
					gun     = _models_to_add.get('gun')
										
					if chassis is not None:
						chassis.position = Math.Vector3(spawn_pos)
						chassis.yaw = 0.0
						_add_model(chassis)
						try:
							chassis.addMotor(BigWorld.Servo(chassis_mp))
							LOG_DEBUG('OfflineBattle.chassis Servo attached')
						except Exception as e:
							LOG_DEBUG('OfflineBattle.chassis Servo error:', str(e))
						
						if hull is not None:
							try:
								chassis.node('V').attach(hull)
								LOG_DEBUG('OfflineBattle: hull attached to chassis.V')
							except Exception as e:
								LOG_DEBUG('OfflineBattle.attach hull error:', str(e))
								hull.position = Math.Vector3(spawn_pos)
								_add_model(hull)
						
							# Attach turret to hull node 'HP_turretJoint'
							if turret is not None:
								try:
									turret_mat = Math.Matrix()
									turret_mat.setIdentity()
									loaded_models['turret_mat'] = turret_mat
									hull.node('HP_turretJoint', turret_mat).attach(turret)
									LOG_DEBUG('OfflineBattle: turret attached to hull.HP_turretJoint')
								except Exception as e:
									LOG_DEBUG('OfflineBattle.attach turret error:', str(e))
								
								# Attach gun to turret node 'HP_gunJoint'
								if gun is not None:
									try:
										gun_mat = Math.Matrix()
										gun_mat.setIdentity()
										loaded_models['gun_mat'] = gun_mat
										turret.node('HP_gunJoint', gun_mat).attach(gun)
										LOG_DEBUG('OfflineBattle: gun attached to turret.HP_gunJoint')
									except Exception as e:
										LOG_DEBUG('OfflineBattle.attach gun error:', str(e))
					elif hull is not None:
						hull.position = Math.Vector3(spawn_pos)
						_add_model(hull)
						LOG_DEBUG('OfflineBattle.addModel OK: hull (no chassis)')
					
					root_model = chassis or hull
					ma.models = [root_model]
					ma.compoundModel = root_model
					LOG_DEBUG('OfflineBattle.compoundModel set, attempt:', _add_attempts[0])


					# Init engine/movement sounds now that model is in scene
					try:
						if not _engine_state['init'] and root_model is not None:
							_engine_state['init'] = True
							td2 = loaded_models.get('td')
							if td2 is not None:
								try: engine_snd_path = td2.chassis['sound']['engine']
								except Exception: engine_snd_path = None
								try: movement_snd_path = td2.chassis['sound']['movement']
								except Exception: movement_snd_path = None
								if engine_snd_path:
									_engine_state['snd1'] = root_model.playSound(engine_snd_path)
									LOG_DEBUG('OfflineBattle.engine snd:', engine_snd_path, _engine_state['snd1'])
								if movement_snd_path:
									_engine_state['snd2'] = root_model.playSound(movement_snd_path)
									LOG_DEBUG('OfflineBattle.movement snd:', movement_snd_path, _engine_state['snd2'])
								if not engine_snd_path and not movement_snd_path:
									LOG_DEBUG('OfflineBattle.no sound paths, chassis keys:', td2.chassis.keys() if hasattr(td2.chassis, 'keys') else type(td2.chassis))
					except Exception:
						import traceback
						LOG_DEBUG('OfflineBattle.engine snd init ERROR:', traceback.format_exc())
				
				except Exception as e:
					import traceback
					LOG_DEBUG('OfflineBattle._add_models_when_ready ERROR:', traceback.format_exc())
					if _add_attempts[0] < 10:
						BigWorld.callback(0.3, _add_models_when_ready)
			
			BigWorld.callback(0.2, _add_models_when_ready)
			
			# Set temporary compoundModel so camera logic doesn't fail
			root_model = loaded_models['chassis'] if loaded_models['chassis'] is not None else loaded_models['hull']
			ma.models = [root_model]
			ma.compoundModel = root_model

		class _MockVeh(object):
			def __init__(self):
				self.matrix = Math.Matrix()
				self.matrix.setIdentity()
				self.position = Math.Vector3(spawn_pos)
				self.yaw = 0.0
				self.pitch = 0.0
				self.roll = 0.0
				self.filter = mf
				self.appearance = ma
				self.isPlayer = True
				self.typeDescriptor = td
				self.health = getattr(td, 'maxHealth', 400)
				self.maxHealth = getattr(td, 'maxHealth', 400)
				self.isStarted = True
				self.id = getattr(player, 'playerVehicleID', 0)
				self.model = getattr(self.appearance, 'compoundModel', None)
				
				class _ModelsDesc(object):
					def __getitem__(self, key):
						if key in loaded_models and loaded_models.get(key) is not None:
							return {'model': loaded_models[key]}
						# Return None model so SniperCamera falls through
						# to the MatrixProduct branch (which uses getOwnVehicleMatrix)
						return {'model': None}
				self.appearance.modelsDesc = _ModelsDesc()
			def isAlive(self): return True
			def getAutorotation(self): return False
			def __getattr__(self, name): return None
			def collideSegment(self, *a, **kw): return None

		mock_veh = _MockVeh()

		mock_vehicles = {getattr(BigWorld.player(), 'playerVehicleID', -1): mock_veh}

		_orig_entity = BigWorld.entity
		def _mock_entity(eid):
			orig_e = _orig_entity(eid)
			if orig_e is None and eid in mock_vehicles:
				return mock_vehicles[eid]
			return orig_e
		BigWorld.entity = _mock_entity

		player.getVehicleAttached = lambda: mock_veh
		player.getOwnVehicleMatrix = lambda: veh_matrix
		player.getOwnVehiclePosition = lambda: mock_veh.position
		player.handleKey = lambda key, isDown, mods=0: None
		player.getAutorotation = lambda: False
		player.enableOwnVehicleAutorotation = lambda val: None

		class FakePositionControl(object):
			def bindToVehicle(self, *a, **k): pass
			def followCamera(self, *a, **k): pass
			def moveTo(self, *a, **k): pass
		player.positionControl = FakePositionControl()

		class FakeStats(object):
			def getCache(self, cb): cb(1, {})
			def __getattr__(self, name): return lambda *a, **k: None
			
		if not hasattr(player, '_offhangar_orig_stats'):
			player._offhangar_orig_stats = getattr(player, 'stats', None)
		player.stats = FakeStats()

		class FakeGunRotator(object):
			def __init__(self):
				self.markerInfo = [(0.0, 0.0, 0.0), (0.0, 0.0, 0.0)]
				self.dispersionAngle = 0.1
			def getShotParams(self, targetPos, *a, **kw):
				import BigWorld, Math
				try:
					speed = BigWorld.player().vehicleTypeDescriptor.shot['speed']
					gravity = BigWorld.player().vehicleTypeDescriptor.shot['gravity']
				except:
					speed, gravity = 250.0, 9.81
				if hasattr(self, '_gun_pos') and hasattr(self, '_gun_dir'):
					if len(a) > 1 and kw.get('gravity') is not None:
						return (self._gun_pos, self._gun_dir.scale(speed), Math.Vector3(0, -gravity, 0))
					return (self._gun_pos, self._gun_dir.scale(speed))
				
				# fallback to default
				import math
				yaw = BigWorld.camera().direction.yaw
				pitch = BigWorld.camera().direction.pitch
				v0 = Math.Vector3(math.sin(yaw)*math.cos(pitch), -math.sin(pitch), math.cos(yaw)*math.cos(pitch))
				startPos = BigWorld.camera().position
				if len(a) > 1 and kw.get('gravity') is not None:
					return (startPos, v0.scale(speed), Math.Vector3(0, -gravity, 0))
				return (startPos, v0.scale(speed))
			def _VehicleGunRotator__getCurShotPosition(self):
				import BigWorld, Math
				try:
					speed = BigWorld.player().vehicleTypeDescriptor.shot['speed']
				except:
					speed = 250.0
				if hasattr(self, '_gun_pos') and hasattr(self, '_gun_dir'):
					return (self._gun_pos, self._gun_dir.scale(speed))
				
				# fallback to default
				import math
				yaw = BigWorld.camera().direction.yaw
				pitch = BigWorld.camera().direction.pitch
				v0 = Math.Vector3(math.sin(yaw)*math.cos(pitch), -math.sin(pitch), math.cos(yaw)*math.cos(pitch))
				startPos = BigWorld.camera().position
				return (startPos, v0.scale(speed))
		player.gunRotator = FakeGunRotator()

		player.getOwnVehicleSpeeds = lambda: (0.0, 0.0)
		player.autoAim = lambda val: None

		if hasattr(player, 'arena') and player.arena is not None:
			if not hasattr(player.arena, 'collideWithSpaceBB') or not callable(getattr(player.arena, 'collideWithSpaceBB', None)):
				player.arena.collideWithSpaceBB = lambda *a, **kw: None

		veh_yaw     = [spawn_dir.z]
		turret_yaw  = [0.0]   # relative to hull
		gun_pitch   = [0.0]   # gun elevation
		veh_pos = [spawn_pos.x, spawn_pos.y, spawn_pos.z]
		turret_matrix = Math.Matrix()
		turret_matrix.setTranslate(Math.Vector3(spawn_pos.x, spawn_pos.y + 2.0, spawn_pos.z))

		# Read turret/gun rotation limits from vehicle descriptor
		_turret_rot_speed = 0.03  # rad per tick default
		_gun_min_pitch    = -0.15  # ~-8 deg default
		_gun_max_pitch    =  0.35  # ~+20 deg default
		_gun_min_yaw      = -3.14159
		_gun_max_yaw      =  3.14159
		try:
			if td is not None:
				rot = td.turret.get('rotationSpeed', None)
				if rot is not None:
					_turret_rot_speed = float(rot) * 0.02  # per 20ms tick
				pl = td.gun.get('pitchLimits', None)
				if pl is not None:
					import math as _math
					mn = pl.get('minPitch', pl.get('minAngle', None))
					mx = pl.get('maxPitch', pl.get('maxAngle', None))
					if mn is not None: _gun_min_pitch = _math.radians(float(mn))
					if mx is not None: _gun_max_pitch = _math.radians(float(mx))
				yl = td.gun.get('turretYawLimits', None)
				if yl is None and td.turret is not None:
					yl = td.turret.get('yawLimits', None)
				if yl is not None:
					import math as _math
					_gun_min_yaw = float(yl[0])
					_gun_max_yaw = float(yl[1])
					if abs(_gun_min_yaw) > 10.0 or abs(_gun_max_yaw) > 10.0:
						_gun_min_yaw = _math.radians(_gun_min_yaw)
						_gun_max_yaw = _math.radians(_gun_max_yaw)
		except Exception as e:
			LOG_DEBUG('OfflineBattle.limits error:', str(e))

		_tick_counter = [0]

		# Engine and track sound state
		_sound_state = {
			'engine_sound': None,
			'tread_sound': None,
			'last_engine_event': '',
			'last_tread_event': '',
		}

		# Determine tank class for sound events
		_tank_class = 'medium'
		try:
			if td is not None:
				tags = td.type.tags if hasattr(td, 'type') and hasattr(td.type, 'tags') else set()
				if 'lightTank' in tags: _tank_class = 'light'
				elif 'heavyTank' in tags: _tank_class = 'heavy'
				elif 'SPG' in tags or 'AT-SPG' in tags: _tank_class = 'SAU'
				else: _tank_class = 'medium'
			LOG_DEBUG('OfflineBattle.tank_class:', _tank_class)
		except Exception as e:
			LOG_DEBUG('OfflineBattle.tank_class error:', str(e))

		# Map tank class to FMOD event prefix
		_engine_idle_event = '/tanks/%s/%s/%s' % (
			{'light': 'light', 'heavy': 'heavy', 'medium': 'medium', 'SAU': 'medium'}.get(_tank_class, 'medium'),
			{'light': 'MC-1', 'heavy': 'IS_2', 'medium': 'tiger', 'SAU': 'tiger'}.get(_tank_class, 'tiger'),
			{'light': 'idle', 'heavy': 'IS_2_stand', 'medium': 'tiger_idle', 'SAU': 'tiger_idle'}.get(_tank_class, 'tiger_idle'),
		)
		_engine_run_event = '/tanks/%s/%s/%s' % (
			{'light': 'light', 'heavy': 'heavy', 'medium': 'medium', 'SAU': 'medium'}.get(_tank_class, 'medium'),
			{'light': 'MC-1', 'heavy': 'IS_2', 'medium': 'tiger', 'SAU': 'tiger'}.get(_tank_class, 'tiger'),
			{'light': 'run', 'heavy': 'heavy_tank_run_state2', 'medium': 'medium_tank_state2', 'SAU': 'medium_tank_state2'}.get(_tank_class, 'medium_tank_state2'),
		)
		_tread_prefix = '/tanks/tanks_treads/%s_tank' % ({'SAU': 'SAU'}.get(_tank_class, _tank_class))

# --- GUN MECHANICS STATE ---
		_gun_state = {
			'base_dispersion': 0.1,
			'after_shot': 1.5,
			'aim_time': 2.0,
			'clip_size': 1,
			'clip_reload': 2.0,
			'reload': 5.0,
			'ammo': 100,
			'clip': 1,
			'reloadTime': 0.0,
			'dispersion': 0.1,
			'initialized': False,
			'shot_index': 0
		}

		_engine_state = {'init': False, 'snd1': None, 'snd2': None}
		
		_veh_velocity = [0.0]        # m/s, forward speed
		_veh_turn_velocity = [0.0]   # rad/s, current hull rotation speed
		_last_tick_time = [BigWorld.time()]
		
		# === WoT-style physics parameters ===
		import math
		_phys_mass           = 5730.0     # kg (total vehicle mass)
		_phys_enginePowerHP  = 45.0       # HP (engine power)
		_phys_speedFwd       = 32.0 / 3.6 # m/s (forward speed limit)
		_phys_speedBwd       = 12.0 / 3.6 # m/s (backward speed limit)
		_phys_chassisRotSpd  = math.radians(38.0) # rad/s (chassis rotation speed)
		_phys_terrainResist  = (1.1, 1.4, 2.6)    # (hard, medium, soft) coefficients
		_phys_specificFriction = 0.6867              # rolling friction coefficient
		
		# Try to read actual values from the vehicle descriptor
		try:
			_tdp = td.physics
			LOG_DEBUG('OfflineBattle.PHYSICS_KEYS:', str(_tdp.keys()) if hasattr(_tdp, 'keys') else str(type(_tdp)))
			
			if 'weight' in _tdp:
				_phys_mass = float(_tdp['weight'])
			if 'enginePower' in _tdp:
				# Engine power in td.physics is already in Watts
				_phys_enginePowerW = float(_tdp['enginePower'])
			else:
				_phys_enginePowerW = _phys_enginePowerHP * 746.0
				
			if 'speedLimits' in _tdp:
				# Speed limits in td.physics are already in m/s
				_phys_speedFwd = float(_tdp['speedLimits'][0])
				_phys_speedBwd = float(_tdp['speedLimits'][1])
			if 'terrainResistance' in _tdp:
				_tr = _tdp['terrainResistance']
				_phys_terrainResist = (float(_tr[0]), float(_tr[1]), float(_tr[2]))
			if 'specificFriction' in _tdp:
				_phys_specificFriction = float(_tdp['specificFriction'])
		except Exception as e:
			LOG_DEBUG('OfflineBattle.physics_read_1:', str(e))
			_phys_enginePowerW = _phys_enginePowerHP * 746.0
		
		# Try to read rotation speed from chassis descriptor
		try:
			if hasattr(td, 'chassis') and 'rotationSpeed' in td.chassis:
				_phys_chassisRotSpd = math.radians(float(td.chassis['rotationSpeed']))
		except Exception as e:
			LOG_DEBUG('OfflineBattle.physics_read_2:', str(e))
		
		# Try rotationSpeedLimit from physics dict
		try:
			if 'rotationSpeedLimit' in _tdp:
				_phys_chassisRotSpd = float(_tdp['rotationSpeedLimit'])
		except Exception:
			pass
		
		# Use hard terrain by default (index 0)
		# Use hard terrain by default (index 0)
		_phys_terrainCoeff = _phys_terrainResist[0]
		# Gravity
		_phys_gravity = 9.81
		
		LOG_DEBUG('OfflineBattle.PHYSICS: mass=%.0f, power=%.0fHP(%.0fW), fwd=%.1f m/s, bwd=%.1f m/s, rot=%.1f deg/s, terrain=(%.2f,%.2f,%.2f), friction=%.4f' % (
			_phys_mass, _phys_enginePowerHP, _phys_enginePowerW, _phys_speedFwd, _phys_speedBwd,
			math.degrees(_phys_chassisRotSpd), _phys_terrainResist[0], _phys_terrainResist[1], _phys_terrainResist[2],
			_phys_specificFriction))
		_battle_finished = [False]
		def _aih_tick():
			try:
				import BigWorld, Math, Keys, math
				player = BigWorld.player()
				
				# Stop the loop if battle is over
				if _battle_finished[0] or player is None:
					return

				current_time = BigWorld.time()
				dt = current_time - _last_tick_time[0]
				_last_tick_time[0] = current_time
				if dt <= 0.0 or dt > 0.5:
					dt = 0.016 # fallback to 60fps

				if not _engine_state['init']:
					try:
						td = loaded_models.get('td')
						root_model = loaded_models.get('chassis') or loaded_models.get('hull') or loaded_models.get('turret') or loaded_models.get('gun')
						if td and hasattr(td, 'engine') and hasattr(td, 'chassis') and root_model is not None and root_model.inWorld:
							_engine_state['snd1'] = root_model.playSound(td.engine['sound'])
							_engine_state['snd2'] = root_model.playSound(td.chassis['sound'])
							_engine_state['init'] = True
							LOG_DEBUG('OfflineBattle: Engine sounds attached!')
					except Exception as e:
						LOG_DEBUG('OfflineBattle: Engine sounds failed:', str(e))

				# --- WoT-style Hull Physics ---
				# Determine input direction
				throttle = 0
				steer = 0
				
				# Allow WASD to move the tank even in Arty Mode, because offline edge-panning is broken
				# and the user needs to be able to rotate the hull to bring targets into the gun arc!
				if getattr(player, '_is_dead', False) is True:
					throttle = 0
					steer = 0
				else:
					if BigWorld.isKeyDown(Keys.KEY_W): throttle = 1
					elif BigWorld.isKeyDown(Keys.KEY_S): throttle = -1
					
					if BigWorld.isKeyDown(Keys.KEY_A): steer = -1
					elif BigWorld.isKeyDown(Keys.KEY_D): steer = 1
				
				# Freeze tank movement if battle hasn't started yet (Prebattle Countdown)
				arena = getattr(BigWorld.player(), 'arena', None)
				if arena is not None and getattr(arena, 'period', 3) < 3:
					throttle = 0
					steer = 0
				
				cur_vel = _veh_velocity[0]
				speed_limit = _phys_speedFwd if throttle >= 0 else _phys_speedBwd
				
				# Engine force: F = P / max(|v|, v_min) — this naturally gives strong
				# initial acceleration that tapers off at high speed (just like WoT)
				engine_force = 0.0
				if throttle != 0:
					min_vel = 1.5  # prevents division by near-zero at standstill
					engine_force = _phys_enginePowerW / max(abs(cur_vel), min_vel)
					# Cap engine force to prevent unrealistic initial thrust on heavy tanks
					max_engine_force = _phys_mass * _phys_gravity * 0.7
					engine_force = min(engine_force, max_engine_force)
					engine_force *= throttle  # direction
				
				# Terrain resistance force (rolling resistance opposes motion):
				# base_track_rr is a typical tracked vehicle rolling resistance coefficient (~0.07)
				base_track_rr = 0.07
				resist_force = _phys_mass * _phys_gravity * _phys_terrainCoeff * base_track_rr
				
				# Apply braking when:
				#  - No throttle (coasting to stop)
				#  - Throttle is opposite to current velocity (active braking)
				braking = False
				if throttle == 0 and abs(cur_vel) > 0.01:
					braking = True
				elif throttle != 0 and ((throttle > 0 and cur_vel < -0.1) or (throttle < 0 and cur_vel > 0.1)):
					braking = True
				
				# Net force calculation
				if braking:
					# Braking force: tracks are locked, so we use specificFriction (sliding friction)
					brake_force = _phys_mass * _phys_gravity * _phys_terrainCoeff * _phys_specificFriction
					if cur_vel > 0:
						net_force = -brake_force + engine_force
					else:
						net_force = brake_force + engine_force
				elif throttle == 0:
					net_force = 0.0
				else:
					# Normal driving: engine minus rolling resistance
					if throttle > 0:
						net_force = engine_force - resist_force
					else:
						net_force = engine_force + resist_force  # engine_force is negative here
				
				# Acceleration (F = ma => a = F/m)
				accel = net_force / _phys_mass
				
				# Integrate velocity
				_veh_velocity[0] += accel * dt
				
				# Clamp to speed limits
				if _veh_velocity[0] > _phys_speedFwd:
					_veh_velocity[0] = _phys_speedFwd
				elif _veh_velocity[0] < -_phys_speedBwd:
					_veh_velocity[0] = -_phys_speedBwd
				
				# Stop completely if very slow and no throttle
				if throttle == 0 and abs(_veh_velocity[0]) < 0.05:
					_veh_velocity[0] = 0.0
					
				# Update engine sounds
				try:
					cur_speed = abs(_veh_velocity[0])
					max_speed = _phys_speedFwd
					load = min(1.0, (cur_speed / max_speed) + 0.2 + (abs(throttle) * 0.3))
					if _engine_state['snd1']:
						p = _engine_state['snd1'].param('load')
						if p: p.seek(load)
					if _engine_state['snd2']:
						p = _engine_state['snd2'].param('speed')
						if p: p.seek(cur_speed / max_speed)
				except:
					pass
				
				# Apply position
				if _veh_velocity[0] != 0.0:
					veh_pos[0] += math.sin(veh_yaw[0]) * _veh_velocity[0] * dt
					veh_pos[2] += math.cos(veh_yaw[0]) * _veh_velocity[0] * dt
				
				# --- Hull Rotation (WoT-style) ---
				turn_dir = 0
				if getattr(player, '_is_dead', False) is not True:
					if BigWorld.isKeyDown(Keys.KEY_A): turn_dir = -1
					if BigWorld.isKeyDown(Keys.KEY_D): turn_dir = 1
				
				# WoT reduces rotation speed on bad terrain and when moving
				# At full speed, rotation is ~60-80% of stationary rotation
				speed_ratio = abs(_veh_velocity[0]) / max(_phys_speedFwd, 0.1)
				rot_speed_modifier = 1.0 / (1.0 + speed_ratio * 0.5)
				# Terrain also affects rotation
				terrain_rot_modifier = 1.0 / _phys_terrainCoeff
				
				max_rot_speed = _phys_chassisRotSpd * rot_speed_modifier * terrain_rot_modifier
				
				# Smooth rotation ramp-up (tracks don't instantly grip)
				target_turn_vel = turn_dir * max_rot_speed
				turn_diff = target_turn_vel - _veh_turn_velocity[0]
				turn_accel = max_rot_speed * 4.0  # ~0.25s to reach full rotation speed
				
				if abs(turn_diff) < turn_accel * dt:
					_veh_turn_velocity[0] = target_turn_vel
				else:
					_veh_turn_velocity[0] += turn_accel * dt * (1 if turn_diff > 0 else -1)
				
				# Stop rotation smoothly when no input
				if turn_dir == 0 and abs(_veh_turn_velocity[0]) < 0.01:
					_veh_turn_velocity[0] = 0.0
				
				if _veh_turn_velocity[0] != 0.0:
					veh_yaw[0] += _veh_turn_velocity[0] * dt
					while veh_yaw[0] > math.pi: veh_yaw[0] -= 2*math.pi
					while veh_yaw[0] < -math.pi: veh_yaw[0] += 2*math.pi

				# --- Terrain resistance (ground snap every tick) ---
				try:
					col = BigWorld.wg_collideSegment(
						BigWorld.player().spaceID,
						Math.Vector3(veh_pos[0], veh_pos[1] + 100.0, veh_pos[2]),
						Math.Vector3(veh_pos[0], veh_pos[1] - 1000.0, veh_pos[2]), 128)
					if col is not None:
						# No +0.5 offset, chassis origin is at 0!
						veh_pos[1] = col[0].y
				except Exception:
					pass

				# --- Turret & Gun Mouse Aiming ---
				try:
					is_sniper = False
					is_arty = False
					aih = getattr(BigWorld.player(), 'inputHandler', None)
					if aih and getattr(aih, '_AvatarInputHandler__isStarted', False):
						ctrl = getattr(aih, 'ctrl', None)
						if ctrl is not None:
							name = ctrl.__class__.__name__
							if name == 'SniperControlMode': is_sniper = True
							if name == 'StrategicControlMode': is_arty = True

					# 1. First compute previous exact gun position
					try:
						td = loaded_models.get('td')
						turretOffs = td.hull['turretPositions'][0] + td.chassis['hullPosition']
						gunOffs = td.turret['gunPosition']
					except:
						turretOffs = Math.Vector3(0, 1.5, 0)
						gunOffs = Math.Vector3(0, 0.4, 1.0)

					turretWorldMatrix = Math.Matrix()
					turretWorldMatrix.setRotateY(turret_yaw[0])
					turretWorldMatrix.translation = turretOffs
					turretWorldMatrix.postMultiply(mock_veh.matrix)
					last_true_gun_pos = turretWorldMatrix.applyPoint(gunOffs)

					# 2. Get exact 3D point the crosshair is looking at
					shot_point = None
					try:
						if aih and getattr(aih, '_AvatarInputHandler__isStarted', False):
							shot_point = aih.getDesiredShotPoint()
					except Exception as e:
						pass
					
					if shot_point is None:
						cam_mat = Math.Matrix(BigWorld.camera().matrix)
						cam_pos = cam_mat.translation
						cam_dir = cam_mat.applyToAxis(2)
						cam_dir.normalise()
						end_pos = cam_pos + cam_dir.scale(1000.0)
						col = BigWorld.wg_collideSegment(BigWorld.player().spaceID, cam_pos, end_pos, 128)
						shot_point = col[0] if col is not None else end_pos

					# 3. Calculate target yaw and pitch
					# Vector from mathematical gun to the target
					dx = shot_point.x - last_true_gun_pos.x
					dz = shot_point.z - last_true_gun_pos.z
					dy = shot_point.y - last_true_gun_pos.y
					dist = math.sqrt(dx*dx + dz*dz)
					
					target_yaw = math.atan2(dx, dz)
					
					if is_arty:
						try:
							shots = td.gun['shots'] if isinstance(td.gun, dict) else getattr(td.gun, 'shots')
							shot = shots[0]
							v = shot['speed'] if isinstance(shot, dict) else getattr(shot, 'speed')
							g = shot['gravity'] if isinstance(shot, dict) else getattr(shot, 'gravity', 9.81)
							
							# In WoT gravity is usually a positive number (like 98.1), but sometimes negative. Ensure positive.
							g = abs(g)
							if g < 0.1: g = 9.81
							
							root = v**4 - g * (g * dist**2 + 2 * dy * v**2)
							if root > 0:
								target_pitch = -math.atan((v**2 - math.sqrt(root)) / (g * dist))
							else:
								target_pitch = -math.pi / 4 # 45 degrees max range fallback
						except Exception as e:
							target_pitch = math.atan2(-dy, dist) # direct fire fallback
							LOG_DEBUG('OfflineBattle Arty Pitch Error:', str(e))
					else:
						target_pitch = math.atan2(-dy, dist)

					# Convert to local turret yaw
					local_target_yaw = target_yaw - veh_yaw[0]
					
					# Normalize angles
					while local_target_yaw > math.pi: local_target_yaw -= 2*math.pi
					while local_target_yaw < -math.pi: local_target_yaw += 2*math.pi
					while turret_yaw[0] > math.pi: turret_yaw[0] -= 2*math.pi
					while turret_yaw[0] < -math.pi: turret_yaw[0] += 2*math.pi
					
					# Clamp to max traverse limits (for SPGs and TDs)
					local_target_yaw = max(_gun_min_yaw, min(_gun_max_yaw, local_target_yaw))
					
					diff_yaw = local_target_yaw - turret_yaw[0]
					if diff_yaw > math.pi: diff_yaw -= 2*math.pi
					if diff_yaw < -math.pi: diff_yaw += 2*math.pi
					
					_gun_state['yaw_penalty'] = abs(diff_yaw) * 0.1
					
					if abs(diff_yaw) < _turret_rot_speed:
						turret_yaw[0] = local_target_yaw
					else:
						turret_yaw[0] += _turret_rot_speed * (1 if diff_yaw > 0 else -1)
						
					# Update pitch
					target_pitch = max(_gun_min_pitch, min(_gun_max_pitch, target_pitch))
					
					diff_pitch = target_pitch - gun_pitch[0]
					if abs(diff_pitch) < 0.05:
						gun_pitch[0] = target_pitch
					else:
						gun_pitch[0] += 0.05 * (1 if diff_pitch > 0 else -1)

					player = BigWorld.player()
					if not hasattr(player, 'addModel'):
						player.addModel = lambda m: _add_model(m)
					if not hasattr(player, 'delModel'):
						player.delModel = lambda m: BigWorld.delModel(m)

					if hasattr(player, 'gunRotator'):
						player.gunRotator.markerInfo[0] = shot_point

					# Mock appearance for SniperCamera to find HP_gunJoint
					if 'gun_node_matrix' not in loaded_models:
						loaded_models['gun_node_matrix'] = Math.Matrix()
					if not hasattr(mock_veh, 'appearance'):
						class FakeAppearance(object):
							def __init__(self):
								class FakeCompound(object):
									def node(self, name):
										if name == 'HP_gunJoint': return loaded_models['gun_node_matrix']
										if name == 'HP_turretJoint': return loaded_models.get('hull').node(name) if loaded_models.get('hull') else None
										return mock_veh.model.node(name)
									@property
									def position(self): return mock_veh.position
									@property
									def matrix(self): return mock_veh.matrix
								self.compoundModel = FakeCompound()
								self.modelsDesc = {'gun': {'model': loaded_models.get('gun')}}
							def changeVisibility(self, modelName, modelVisible, attachmentsVisible):
								is_sniper = not modelVisible
								c_mdl = loaded_models.get('chassis')
								h_mdl = loaded_models.get('hull')
								t_mdl = loaded_models.get('turret')
								g_mdl = loaded_models.get('gun')
								if hasattr(c_mdl, 'visible'): c_mdl.visible = not is_sniper
								if hasattr(h_mdl, 'visible'): h_mdl.visible = not is_sniper
								if hasattr(t_mdl, 'visible'): t_mdl.visible = not is_sniper
								if hasattr(g_mdl, 'visible'): g_mdl.visible = not is_sniper
							def hideIfExistFor(self, vehicle):
								pass
						mock_veh.appearance = FakeAppearance()
					
					# Debug log every 50 ticks (1 sec)
					_tick_counter[0] += 1
					if _tick_counter[0] % 50 == 0:
						try:
							cur_cam = Math.Matrix(BigWorld.camera().matrix)
							c_ptc = -cur_cam.pitch
						except:
							c_ptc = 0.0
						LOG_DEBUG('OfflineBattle.aim: cam_yaw=%.2f, veh_yaw=%.2f, loc_tgt=%.2f, tur_yaw=%.2f, cam_ptc=%.2f, gun_ptc=%.2f' % (
							target_yaw, veh_yaw[0], local_target_yaw, turret_yaw[0], c_ptc, gun_pitch[0]))
						
				except Exception as e:
					LOG_DEBUG('OfflineBattle.aim error:', str(e))


				# --- Update mock vehicle and camera matrix ---
				mock_veh.position = Math.Vector3(veh_pos[0], veh_pos[1], veh_pos[2])
				mock_veh.yaw   = veh_yaw[0]
				
				# Update base matrix IN PLACE so AvatarInputHandler doesn't lose the reference
				mock_veh.matrix.setRotateY(veh_yaw[0])
				mock_veh.matrix.translation = mock_veh.position
				
				if hasattr(mock_veh, 'filter'):
					mock_veh.filter.position = mock_veh.position
					mock_veh.filter.yaw = veh_yaw[0]
					
				# Update camera matrix (needs both translation AND yaw for SniperCamera offsets to work)
				# (Arcade camera strips yaw using WGTranslationOnlyMP later)
				new_m = Math.Matrix()
				new_m.setRotateY(veh_yaw[0])
				new_m.translation = mock_veh.position
				veh_matrix.a = new_m

				# Update chassis matrix (position + yaw) - Servo drives the model
				chassis_new = Math.Matrix()
				chassis_new.setRotateY(veh_yaw[0])
				chassis_new.translation = mock_veh.position
				chassis_mp.a = chassis_new

				# --- Engine and track sounds via td.chassis['sound'] ---
				if not _engine_state['init'] and (loaded_models.get('chassis') or loaded_models.get('hull')):
					_engine_state['init'] = True
					root_model = loaded_models.get('chassis') or loaded_models.get('hull')
					try:
						td2 = loaded_models.get('td')
						if td2 is not None:
							engine_snd_path = None
							movement_snd_path = None
							try: engine_snd_path = td2.chassis['sound']['engine']
							except Exception: pass
							try: movement_snd_path = td2.chassis['sound']['movement']
							except Exception: pass
							if engine_snd_path:
								_engine_state['snd1'] = root_model.playSound(engine_snd_path)
								LOG_DEBUG('OfflineBattle.engine snd:', engine_snd_path, _engine_state['snd1'])
							if movement_snd_path:
								_engine_state['snd2'] = root_model.playSound(movement_snd_path)
								LOG_DEBUG('OfflineBattle.movement snd:', movement_snd_path, _engine_state['snd2'])
							if not engine_snd_path and not movement_snd_path:
								LOG_DEBUG('OfflineBattle.sound: no sound paths in td.chassis[sound]')
								LOG_DEBUG('OfflineBattle.chassis keys:', td2.chassis.keys() if hasattr(td2.chassis, 'keys') else type(td2.chassis))
					except Exception:
						import traceback
						LOG_DEBUG('OfflineBattle.engine init ERROR:', traceback.format_exc())


										
						

				# --- Update Gun Mechanics (Dispersion & Reload) ---
				if not _gun_state['initialized']:
					td = loaded_models.get('td')
					if td is not None and hasattr(td, 'gun'):
						try:
							_gun_state['base_dispersion'] = td.gun.get('shotDispersionAngle', 0.1) if isinstance(td.gun, dict) else getattr(td.gun, 'shotDispersionAngle', 0.1)
							if 'shotDispersionFactors' in td.gun if isinstance(td.gun, dict) else hasattr(td.gun, 'shotDispersionFactors'):
								_gun_state['after_shot'] = td.gun['shotDispersionFactors'].get('afterShot', 1.5) if isinstance(td.gun, dict) else td.gun.shotDispersionFactors.get('afterShot', 1.5)
							_gun_state['aim_time'] = td.gun.get('aimingTime', 2.0) if isinstance(td.gun, dict) else getattr(td.gun, 'aimingTime', 2.0)
							if 'clip' in td.gun if isinstance(td.gun, dict) else hasattr(td.gun, 'clip'):
								_clip = td.gun['clip'] if isinstance(td.gun, dict) else td.gun.clip
								_gun_state['clip_size'] = _clip[0]
								_gun_state['clip_reload'] = _clip[1]
							_gun_state['reload'] = td.gun.get('reloadTime', 5.0) if isinstance(td.gun, dict) else getattr(td.gun, 'reloadTime', 5.0)
							
							_gun_state['ammo'] = 45
							if hasattr(td, 'maxAmmo'): _gun_state['ammo'] = td.maxAmmo
							elif isinstance(td.gun, dict) and 'maxAmmo' in td.gun: _gun_state['ammo'] = td.gun['maxAmmo']
							elif hasattr(td.gun, 'maxAmmo'): _gun_state['ammo'] = td.gun.maxAmmo
							elif hasattr(td, 'turret') and hasattr(td.turret, 'maxAmmo'): _gun_state['ammo'] = td.turret.maxAmmo
						except Exception as e:
							LOG_DEBUG('OfflineBattle: Gun State Init ERROR:', str(e))
						_gun_state['clip'] = _gun_state['clip_size']
						_gun_state['dispersion'] = _gun_state['base_dispersion']
						_gun_state['initialized'] = True
						LOG_DEBUG('OfflineBattle: Gun State initialized from TD: dispersion=%.3f, aim_time=%.2f, reload=%.2f, clip_size=%d' % (
							_gun_state['base_dispersion'], _gun_state['aim_time'], _gun_state['reload'], _gun_state['clip_size']))

				if _gun_state['initialized']:
					try:
						# 1. Dispersion shrinkage
	
						if 'GUI_INIT' not in _gun_state:
							try:
								from gui import WindowsManager
								panel = getattr(WindowsManager.g_windowsManager.battleWindow, 'consumablesPanel', None) if getattr(WindowsManager.g_windowsManager, 'battleWindow', None) else None
								if panel:
									try:
										td = loaded_models.get('td')
										shots = td.gun['shots'] if isinstance(td.gun, dict) else getattr(td.gun, 'shots', [])
										
										# Distribute maxAmmo across available shells
										ammo_pool = _gun_state['ammo']
										for i, shot in enumerate(shots):
											try: shell = shot['shell']
											except: shell = getattr(shot, 'shell', None)
											try: piercing_val = shot['piercingPower']
											except: piercing_val = getattr(shot, 'piercingPower', 100)
											if isinstance(piercing_val, (tuple, list)): piercing_val = piercing_val[0]
											
											# Give ~60% to AP, 30% to APCR, 10% to HE roughly
											qty = int(ammo_pool * 0.6) if i == 0 else (int(ammo_pool * 0.3) if i == 1 else int(ammo_pool * 0.1))
											if qty == 0 and ammo_pool > 0: qty = 1
											_gun_state['ammo_%d' % i] = qty
											panel.addShellSlot(i, qty, _gun_state['clip_size'], _gun_state['clip_size'], shell, piercing_val)
											
										# Select the first shell as active to show clip UI
										panel.setCurrentShell(0)
										panel.setShellQuantityInSlot(0, _gun_state['ammo_0'], _gun_state['clip'])
									except Exception as ex: LOG_DEBUG('SHELL SLOT FAIL:', str(ex))
									
									try:
										aim = getattr(g_offline_aih, 'aim', None)
										if aim:
											aim.setClipParams(_gun_state['clip_size'], 1)
											aim.setAmmoStock(_gun_state['ammo_0'], _gun_state['clip'], False)
									except Exception as e: pass
									
									_gun_state['GUI_INIT'] = True
									LOG_DEBUG('OfflineBattle: GUI panel initialized!')
							except Exception as e:
								LOG_DEBUG('OfflineBattle GUI Init Error:', str(e))
						cur_time = BigWorld.time()
						if 'last_time' not in _gun_state: _gun_state['last_time'] = cur_time
						dt = cur_time - _gun_state['last_time']
						_gun_state['last_time'] = cur_time
						
						target_disp = _gun_state['base_dispersion']
						penalty = _gun_state.get('yaw_penalty', 0.0)
						target_disp += penalty
						
						if _gun_state['dispersion'] > target_disp:
							shrink_rate = (_gun_state['dispersion'] - target_disp) * (dt / max(_gun_state['aim_time'], 0.1))
							_gun_state['dispersion'] = max(target_disp, _gun_state['dispersion'] - shrink_rate)
						else:
							_gun_state['dispersion'] = min(_gun_state['dispersion'] + (target_disp - _gun_state['dispersion']) * 0.2, 5.0)

						# 2. Reload logic
						if _gun_state['reloadTime'] > 0:
							_gun_state['reloadTime'] -= dt
							if _gun_state['reloadTime'] <= 0:
								_gun_state['reloadTime'] = 0.0
								if _gun_state['clip'] == 0:
									_gun_state['clip'] = _gun_state['clip_size']
								
								# Reset UI cooldown and refresh ammo count when reload finishes
								try:
									from gui import WindowsManager
									panel = WindowsManager.g_windowsManager.battleWindow.consumablesPanel
									if panel:
										panel.setShellQuantityInSlot(0, _gun_state['ammo_0'], _gun_state['clip'])
										panel.setCoolDownTime(0, 0.0)
									aim = getattr(g_offline_aih, 'aim', None)
									if aim:
										aim.setReloading(0.0, None)
										aim.setAmmoStock(_gun_state['ammo_0'], _gun_state['clip'], True if _gun_state['clip'] == _gun_state['clip_size'] else False)
									
									try:
										if not hasattr(BigWorld.player(), 'soundNotifications'):
											import gui.IngameSoundNotifications as IngameSoundNotifications
											BigWorld.player().soundNotifications = IngameSoundNotifications.IngameSoundNotifications()
											BigWorld.player().soundNotifications.start()
										BigWorld.player().soundNotifications.play('gun_reloaded')
									except: pass
								except Exception:
									pass
					except Exception as e:
						LOG_DEBUG('OfflineBattle dispersion error:', str(e))

					# 3. Update Crosshair + AIH
					try:
						# Let the engine update the aim crosshair
						# Compute where the gun is actually pointing (offset start pos by 4.0m to avoid hitting our own tank hull!)
						try:
							td = loaded_models.get('td')
							turretOffs = td.hull['turretPositions'][0] + td.chassis['hullPosition']
							gunOffs = td.turret['gunPosition']
						except:
							turretOffs = Math.Vector3(0, 1.5, 0)
							gunOffs = Math.Vector3(0, 0.4, 1.0)

						turretWorldMatrix = Math.Matrix()
						turretWorldMatrix.setRotateY(turret_yaw[0])
						turretWorldMatrix.translation = turretOffs
						turretWorldMatrix.postMultiply(mock_veh.matrix)

						true_gun_pos = turretWorldMatrix.applyPoint(gunOffs)

						gunWorldMatrix = Math.Matrix()
						gunWorldMatrix.setRotateX(gun_pitch[0])
						gunWorldMatrix.translation = gunOffs
						gunWorldMatrix.postMultiply(turretWorldMatrix)
						if 'gun_node_matrix' in loaded_models:
							gunJointMatrix = Math.Matrix()
							gunJointMatrix.setRotateY(veh_yaw[0])
							gunJointMatrix.translation = turretWorldMatrix.applyPoint(gunOffs)
							loaded_models['gun_node_matrix'].set(gunJointMatrix)
						
						yaw_total = veh_yaw[0] + turret_yaw[0]
						pitch_total = gun_pitch[0]
						
						dx = math.sin(yaw_total) * math.cos(pitch_total)
						dy = -math.sin(pitch_total)
						dz = math.cos(yaw_total) * math.cos(pitch_total)
						gun_dir = Math.Vector3(dx, dy, dz)
						gun_dir.normalise()
						
						# Pass gun pos to rotator for Arty/Arcade raycasts
						if hasattr(player, 'gunRotator'):
							player.gunRotator._gun_pos = true_gun_pos
							player.gunRotator._gun_dir = gun_dir
							
						is_arty = False
						try: is_arty = 'SPG' in td.type.tags
						except: pass
							
						if is_arty:
							# Simulate arty ballistic trajectory hit point at current turret yaw
							dx_aim = shot_point.x - true_gun_pos.x
							dz_aim = shot_point.z - true_gun_pos.z
							dist_aim = math.sqrt(dx_aim*dx_aim + dz_aim*dz_aim)
							
							current_gun_yaw = turret_yaw[0] + veh_yaw[0]
							land_x = true_gun_pos.x + math.sin(current_gun_yaw) * dist_aim
							land_z = true_gun_pos.z + math.cos(current_gun_yaw) * dist_aim
							
							# Find terrain height
							col_down = BigWorld.wg_collideSegment(BigWorld.player().spaceID, 
								Math.Vector3(land_x, 1000.0, land_z), 
								Math.Vector3(land_x, -1000.0, land_z), 128)
							if col_down is not None:
								gun_target_pos = col_down[0]
							else:
								gun_target_pos = Math.Vector3(land_x, shot_point.y, land_z)
						else:
							# Offset by 2.0 to avoid hitting our own hull
							gun_pos = true_gun_pos + gun_dir.scale(2.0)
							gun_end = gun_pos + gun_dir.scale(10000.0)
							gun_hit = BigWorld.wg_collideSegment(player.spaceID, gun_pos, gun_end, 128)
							gun_target_pos = gun_hit[0] if gun_hit is not None else gun_end
						
						if _tick_counter[0] % 50 == 0:
							LOG_DEBUG('OfflineBattle.gun: target_pos=', gun_target_pos, 'dir=', gun_dir, 'pos=', true_gun_pos)
							
						# Hide vehicle in sniper mode
						if hasattr(g_offline_aih, 'ctrl'):
							is_sniper = g_offline_aih.ctrl.__class__.__name__ == 'SniperControlMode'
							c_mdl = loaded_models.get('chassis')
							h_mdl = loaded_models.get('hull')
							t_mdl = loaded_models.get('turret')
							g_mdl = loaded_models.get('gun')
							if hasattr(c_mdl, 'visible'): c_mdl.visible = not is_sniper
							if hasattr(h_mdl, 'visible'): h_mdl.visible = not is_sniper
							if hasattr(t_mdl, 'visible'): t_mdl.visible = not is_sniper
							if hasattr(g_mdl, 'visible'): g_mdl.visible = not is_sniper

						# UPDATE CROSSHAIR
						if hasattr(g_offline_aih, 'ctrl'):
							try:
								g_offline_aih.ctrl.updateGunMarker(gun_target_pos, gun_dir, _gun_state['dispersion'], 0.1, None)
							except Exception as e:
								LOG_DEBUG('OfflineBattle updateGunMarker error:', str(e), 'pos:', gun_pos, 'dir:', gun_dir)
							try:
								g_offline_aih.ctrl.updateGunMarker2(gun_target_pos, gun_dir, _gun_state['dispersion'], 0.1, None)
							except Exception as e:
								pass
								
							# Synchronize ammo UI when switching control modes
							aim = getattr(g_offline_aih, 'aim', None)
							if aim and aim != _gun_state.get('last_aim'):
								_gun_state['last_aim'] = aim
								try:
									if hasattr(aim, 'setClipParams'): aim.setClipParams(_gun_state['clip_size'], 1)
									if hasattr(aim, 'setAmmoStock'): aim.setAmmoStock(_gun_state['ammo_0'], _gun_state['clip'], False)
									if _gun_state['reloadTime'] > 0 and hasattr(aim, 'setReloading'): aim.setReloading(_gun_state['reloadTime'], None)
								except Exception: pass
					except Exception as e:
						import traceback
						LOG_DEBUG('OfflineBattle fatal gun error:', traceback.format_exc())
						
				# Update turret rotation (via node matrix)
				turret_mat = loaded_models.get('turret_mat')
				if turret_mat is not None:
					turret_mat.setRotateYPR((turret_yaw[0], 0, 0))

				# Update gun pitch (via node matrix)
				gun_mat = loaded_models.get('gun_mat')
				if gun_mat is not None:
					gun_mat.setRotateYPR((0, gun_pitch[0], 0))

				
						
				# --- Update turret_matrix for camera/AIH ---
				tm = Math.Matrix()
				tm.setRotateYPR((veh_yaw[0] + turret_yaw[0], gun_pitch[0], 0))
				try:
					td = loaded_models.get('td')
					turret_offs = td.hull['turretPositions'][0] + td.chassis['hullPosition']
					tm.translation = mock_veh.matrix.applyPoint(turret_offs)
				except:
					tm.translation = Math.Vector3(veh_pos[0], veh_pos[1] + 2.0, veh_pos[2])
				turret_matrix.set(tm)

				# --- BOT AI (Advanced Physics) ---
				import math, random
				dt = 0.02 # approx tick delta
				for eid, m_veh in mock_vehicles.iteritems():
					if eid != getattr(player, 'playerVehicleID', -1) and getattr(m_veh, 'isAlive', False):
						try:
							dx = veh_pos[0] - m_veh.position.x
							dz = veh_pos[2] - m_veh.position.z
							dist = math.sqrt(dx*dx + dz*dz)
							_td = getattr(m_veh, 'typeDescriptor', None) or loaded_models.get('td')
							
							# INIT BOT STATES
							if getattr(m_veh, '_veh_velocity', None) is None: m_veh._veh_velocity = 0.0
							if getattr(m_veh, '_veh_turn_velocity', None) is None: m_veh._veh_turn_velocity = 0.0
							
							# READ PHYSICS
							bot_mass = 30000.0
							bot_enginePowerW = 500000.0
							bot_speedFwd = 10.0
							bot_speedBwd = 5.0
							bot_terrainCoeff = 1.0
							bot_specificFriction = 0.8
							bot_chassisRotSpd = 0.5
							
							try:
								if _td:
									if 'weight' in _td.physics: bot_mass = float(_td.physics['weight'])
									if 'enginePower' in _td.physics: bot_enginePowerW = float(_td.physics['enginePower'])
									if 'speedLimits' in _td.physics:
										bot_speedFwd = float(_td.physics['speedLimits'][0])
										bot_speedBwd = float(_td.physics['speedLimits'][1])
									if 'terrainResistance' in _td.physics: bot_terrainCoeff = float(_td.physics['terrainResistance'][0])
									if 'specificFriction' in _td.physics: bot_specificFriction = float(_td.physics['specificFriction'])
									if hasattr(_td, 'chassis') and 'rotationSpeed' in _td.chassis:
										bot_chassisRotSpd = math.radians(float(_td.chassis['rotationSpeed']))
									elif 'rotationSpeedLimit' in _td.physics:
										bot_chassisRotSpd = float(_td.physics['rotationSpeedLimit'])
							except: pass
							
							# VIRTUAL DRIVER
							throttle = 0.0
							turn_dir = 0
							
							target_yaw = math.atan2(dx, dz)
							diff_yaw = target_yaw - m_veh.yaw
							while diff_yaw > math.pi: diff_yaw -= 2*math.pi
							while diff_yaw < -math.pi: diff_yaw += 2*math.pi
							
							if dist > 15.0:
								if abs(diff_yaw) < 0.5: throttle = 1.0 # full ahead
								elif abs(diff_yaw) > 2.0: throttle = -1.0 # back up
								else: throttle = 0.5
							
							if diff_yaw > 0.05: turn_dir = 1
							elif diff_yaw < -0.05: turn_dir = -1
							
							# ACCELERATION & MOVEMENT
							bot_gravity = 9.81
							cur_vel = m_veh._veh_velocity
							engine_force = 0.0
							if throttle != 0:
								engine_force = bot_enginePowerW / max(abs(cur_vel), 1.5)
								max_engine_force = bot_mass * bot_gravity * 0.7
								engine_force = min(engine_force, max_engine_force) * throttle
								
							base_track_rr = 0.07
							resist_force = bot_mass * bot_gravity * bot_terrainCoeff * base_track_rr
							
							braking = False
							if throttle == 0 and abs(cur_vel) > 0.01: braking = True
							elif throttle != 0 and ((throttle > 0 and cur_vel < -0.1) or (throttle < 0 and cur_vel > 0.1)): braking = True
							
							if braking:
								brake_force = bot_mass * bot_gravity * bot_terrainCoeff * bot_specificFriction
								if cur_vel > 0: net_force = -brake_force + engine_force
								else: net_force = brake_force + engine_force
							elif throttle == 0: net_force = 0.0
							else:
								if throttle > 0: net_force = engine_force - resist_force
								else: net_force = engine_force + resist_force
								
							accel = net_force / bot_mass
							m_veh._veh_velocity += accel * dt
							if m_veh._veh_velocity > bot_speedFwd: m_veh._veh_velocity = bot_speedFwd
							elif m_veh._veh_velocity < -bot_speedBwd: m_veh._veh_velocity = -bot_speedBwd
							
							if throttle == 0 and abs(m_veh._veh_velocity) < 0.05: m_veh._veh_velocity = 0.0
							
							if m_veh._veh_velocity != 0.0:
								m_veh.position = Math.Vector3(
									m_veh.position.x + math.sin(m_veh.yaw) * m_veh._veh_velocity * dt,
									m_veh.position.y,
									m_veh.position.z + math.cos(m_veh.yaw) * m_veh._veh_velocity * dt
								)
								
							# ROTATION
							speed_ratio = abs(m_veh._veh_velocity) / max(bot_speedFwd, 0.1)
							rot_speed_modifier = 1.0 / (1.0 + speed_ratio * 0.5)
							terrain_rot_modifier = 1.0 / bot_terrainCoeff
							max_rot_speed = bot_chassisRotSpd * rot_speed_modifier * terrain_rot_modifier
							
							target_turn_vel = turn_dir * max_rot_speed
							turn_diff = target_turn_vel - m_veh._veh_turn_velocity
							turn_accel = max_rot_speed * 4.0
							
							if abs(turn_diff) < turn_accel * dt: m_veh._veh_turn_velocity = target_turn_vel
							else: m_veh._veh_turn_velocity += turn_accel * dt * (1 if turn_diff > 0 else -1)
							
							if turn_dir == 0 and abs(m_veh._veh_turn_velocity) < 0.01: m_veh._veh_turn_velocity = 0.0
							
							if m_veh._veh_turn_velocity != 0.0:
								m_veh.yaw += m_veh._veh_turn_velocity * dt
								while m_veh.yaw > math.pi: m_veh.yaw -= 2*math.pi
								while m_veh.yaw < -math.pi: m_veh.yaw += 2*math.pi
							
							# TERRAIN SNAP
							try:
								ground_hit = BigWorld.wg_collideSegment(player.spaceID, Math.Vector3(m_veh.position.x, 1000.0, m_veh.position.z), Math.Vector3(m_veh.position.x, -1000.0, m_veh.position.z), 128)
								if ground_hit:
									m_veh.position = Math.Vector3(m_veh.position.x, ground_hit[0].y, m_veh.position.z)
							except: pass
							
							m_veh.matrix.setRotateY(m_veh.yaw)
							m_veh.matrix.translation = m_veh.position
							
							if hasattr(m_veh, '_chassis_model'):
								if not getattr(m_veh, '_servo_added', False):
									try:
										m_veh._chassis_model.addMotor(BigWorld.Servo(m_veh.matrix))
										m_veh._servo_added = True
									except: pass
									
							# Otaceni veze nezavisle
							if hasattr(m_veh, '_t_mat'):
								t_yaw = target_yaw - m_veh.yaw
								while t_yaw > math.pi: t_yaw -= 2*math.pi
								while t_yaw < -math.pi: t_yaw += 2*math.pi
								
								if getattr(m_veh, '_turret_yaw', None) is None: m_veh._turret_yaw = 0.0
								t_diff = t_yaw - m_veh._turret_yaw
								rot_speed = 0.5
								try:
									if _td: rot_speed = _td.turret['rotationSpeed']
								except: pass
								rot_step = rot_speed * dt
								
								if t_diff > rot_step: m_veh._turret_yaw += rot_step
								elif t_diff < -rot_step: m_veh._turret_yaw -= rot_step
								else: m_veh._turret_yaw = t_yaw
								
								m_veh._t_mat.setRotateYPR((m_veh._turret_yaw, 0, 0))
									
							# Strelba bota na hrace
							if getattr(m_veh, '_ai_shoot_timer', None) is None: m_veh._ai_shoot_timer = 0
							m_veh._ai_shoot_timer += dt
							
							bot_reload = 3.0
							try:
								if _td and hasattr(_td, 'gun') and 'reloadTime' in _td.gun:
									bot_reload = float(_td.gun['reloadTime'])
								elif isinstance(getattr(_td, 'gun', None), dict) and 'reloadTime' in _td.gun:
									bot_reload = float(_td.gun['reloadTime'])
							except: pass
							
							if m_veh._ai_shoot_timer > bot_reload and dist < 150.0:
								m_veh._ai_shoot_timer = 0
								try:
									if g_projectile_mover and _td:
										from items import vehicles
										_shots = _td.gun['shots'] if hasattr(_td, 'gun') and 'shots' in _td.gun else []
										if not _shots and isinstance(_td.gun, dict): _shots = _td.gun.get('shots', [])
										if _shots:
											_shot = _shots[0]
											_effectsDescr = vehicles.g_cache.shotEffects[_shot['shell']['effectsIndex']]
											_gravity = _shot['gravity']
											_speed = _shot['speed']
											
											dir_v = Math.Vector3(dx, (veh_pos[1]+1.0) - (m_veh.position.y+1.5), dz)
											dir_v.normalise()
											_vel = dir_v.scale(_speed)
											
											start_p = Math.Vector3(m_veh.position.x, m_veh.position.y + 1.5, m_veh.position.z)
											_cam_pos = BigWorld.camera().position if BigWorld.camera() else start_p
											g_projectile_mover.add(random.randint(10000, 99999), _effectsDescr, _gravity, start_p, _vel, start_p, True, _cam_pos)
											
											player_mock = mock_vehicles.get(getattr(player, 'playerVehicleID', -1))
											if player_mock and getattr(player_mock, 'health', 0) > 0:
												dmg = random.randint(int(_shot['shell']['damage'][0]*0.75), int(_shot['shell']['damage'][0]*1.25))
												player_mock.health -= dmg
												if player_mock.health <= 0:
													player_mock.health = 0
												
												# Update player vehicle HP physically
												if hasattr(player, 'vehicle') and player.vehicle:
													player.vehicle.health = player_mock.health
													
												# Update GUI
												try:
													import gui.WindowsManager
													bw = gui.WindowsManager.g_windowsManager.battleWindow
													if hasattr(bw, 'damagePanel'):
														bw.damagePanel.updateHealth(player_mock.health)
													if hasattr(bw, 'vMarkersManager'):
														bw.vMarkersManager.updateVehicleHealth(player.playerVehicleID, player_mock.health, 1, 0)
												except: pass
								except Exception: pass
						except Exception as e:
							import traceback
							LOG_DEBUG('Bot AI Exception:', traceback.format_exc())
							
				# PLAYER DEATH CHECK
				try:
					player_mock = mock_vehicles.get(getattr(player, 'playerVehicleID', -1))
					if player_mock and player_mock.health <= 0 and getattr(player, '_is_dead', False) is not True:
						player._is_dead = True
						LOG_DEBUG('Player is dead. Spawning destroyed model and ending battle.')
						
						# Swap model - hide live models, show destroyed ones
						try:
							_dtd = getattr(player_mock, 'typeDescriptor', None) or loaded_models.get('td')
							_d_ch = BigWorld.Model(_dtd.chassis['models']['destroyed'])
							_d_hu = BigWorld.Model(_dtd.hull['models']['destroyed'])
							_d_tu = BigWorld.Model(_dtd.turret['models']['destroyed'])
							_d_gu = BigWorld.Model(_dtd.gun['models']['destroyed'])
							
							def _swap_player_destroyed(_d_ch=_d_ch, _d_hu=_d_hu, _d_tu=_d_tu, _d_gu=_d_gu):
								try:
									# Step 1: Remove live chassis from scene (hull/turret/gun are attached children)
									_live_chassis = loaded_models.get('chassis') or loaded_models.get('hull')
									if _live_chassis is not None:
										try:
											for _mot in list(_live_chassis.motors):
												_live_chassis.delMotor(_mot)
										except: pass
										try: BigWorld.delModel(_live_chassis)
										except: pass
									
									# Step 2: Attach destroyed sub-parts
									try: _d_ch.node('V').attach(_d_hu)
									except: pass
									try: _d_hu.node('HP_turretJoint').attach(_d_tu)
									except: pass
									try: _d_tu.node('HP_gunJoint').attach(_d_gu)
									except: pass
									
									# Step 3: Add destroyed chassis to scene at same position
									_d_ch.position = Math.Vector3(mock_veh.position)
									_add_model(_d_ch)
									try: _d_ch.addMotor(BigWorld.Servo(chassis_mp))
									except: pass
									LOG_DEBUG('Player destroyed model placed OK')
								except Exception as _e:
									import traceback
									LOG_DEBUG('Player model swap failed:', traceback.format_exc())
							
							BigWorld.callback(0.5, _swap_player_destroyed)
						except Exception as _e: LOG_DEBUG('Player death model err:', str(_e))
						
						# Exit battle in 5 seconds - use game.fini() which is the proper hook
						def _exit_battle():
							try:
								LOG_DEBUG('Player death: triggering exit to hangar')
								_battle_finished[0] = True
								
								# Safely stop all control_modes ticks before they can crash
								try:
									_aih = getattr(player, 'inputHandler', None)
									if _aih is not None:
										try: _aih._AvatarInputHandler__isStarted = False
										except: pass
										for _cm in getattr(_aih, '_AvatarInputHandler__ctrls', {}).values():
											try: _cm.destroy()
											except: pass
										try:
											import game
											if hasattr(_aih, '_AvatarInputHandler__onRecreateDevice'):
												game.g_guiResetters.remove(_aih._AvatarInputHandler__onRecreateDevice)
										except: pass
										try: player.inputHandler = None
										except: pass
								except Exception as e:
									import traceback
									LOG_DEBUG('Failed to stop AIH:', traceback.format_exc())
								
								import gui.mods.offhangar._constants as _c
								from gui import WindowsManager
								
								try:
									if hasattr(WindowsManager.g_windowsManager, 'destroyBattle'):
										WindowsManager.g_windowsManager.destroyBattle()
									else:
										WindowsManager.g_windowsManager.hideAll()
								except Exception:
									pass
									
								try:
									global g_offline_models
									for m in list(g_offline_models):
										try: BigWorld.delModel(m)
										except Exception: pass
									g_offline_models = []
								except Exception: pass
								
								try:
									global g_projectile_mover
									if g_projectile_mover is not None:
										g_projectile_mover.destroy()
								except Exception: pass
								
								try:
									BigWorld.camera(None)
									BigWorld.worldDrawEnabled(False)
								except Exception: pass
								
								try:
									import gui.ClientHangarSpace
									LOG_DEBUG('ClientHangarSpace module dir:', dir(gui.ClientHangarSpace))
									LOG_DEBUG('ClientHangarSpace class dir:', dir(gui.ClientHangarSpace.ClientHangarSpace))
								except Exception as e:
									LOG_DEBUG('ClientHangarSpace error:', e)
								
								try:
									BigWorld.worldDrawEnabled(True)
								except Exception: pass
									
								try:
									import gui.ClientHangarSpace
									LOG_DEBUG('Type of CHS:', type(gui.ClientHangarSpace.ClientHangarSpace))
									
									import types
									_chs = None
									for k, v in gui.ClientHangarSpace.__dict__.items():
										try:
											if hasattr(v, 'create') and hasattr(v, 'recreateVehicle'):
												if isinstance(v, type) or isinstance(v, types.ClassType):
													LOG_DEBUG('Found CHS CLASS:', k)
													_chs = v()
													gui.ClientHangarSpace.g_clientHangarSpaceOverride = _chs
													break
										except Exception: pass
									
									if _chs is not None:
										LOG_DEBUG('CHS instance dir:', dir(_chs))
										try: _chs.destroy()
										except Exception as e: LOG_DEBUG('CHS destroy err:', e)
										
										try:
											import inspect
											argspec = inspect.getargspec(_chs.create)
											args_needed = len(argspec.args) - 1  # subtract 'self'
											call_args = [getattr(player, 'isPremium', False)]
											while len(call_args) < args_needed:
												call_args.append(None)
											LOG_DEBUG('Calling CHS create with args:', call_args)
											_chs.create(*call_args)
										except Exception as e: LOG_DEBUG('CHS create err:', e)
										
										_space_id = getattr(_chs, '_ClientHangarSpace__spaceId', None)
										LOG_DEBUG('CHS private spaceId:', _space_id)
										
										if _space_id is not None:
											if BigWorld.camera() is None:
												BigWorld.camera(BigWorld.FreeCamera())
											BigWorld.camera().spaceID = _space_id
								except Exception as e:
									import traceback
									LOG_DEBUG('ClientHangarSpace reset error:', traceback.format_exc())
								
								try:
									BigWorld.worldDrawEnabled(True)
								except Exception: pass
									
								# Set the allow flag and trigger native exit
								for _e in BigWorld.entities.values():
									if _e.__class__.__name__ in ('PlayerAccount', 'Account'):
										_e._offline_allow_become_non_player = True
										if hasattr(_e, '_offhangar_orig_stats') and _e._offhangar_orig_stats is not None:
											_e.stats = _e._offhangar_orig_stats
										try: _e.showGUI(_c.OFFLINE_GUI_CTX)
										except Exception: pass
							except Exception as e:
								import traceback
								LOG_DEBUG('Player exit battle err:', traceback.format_exc())
						
						BigWorld.callback(3.0, _exit_battle)
				except Exception as e: LOG_DEBUG('Player death check err:', str(e))

				BigWorld.callback(0.0, _aih_tick)
			except Exception as e:
				import traceback
				LOG_DEBUG('AIH_TICK CRASH:', traceback.format_exc())
				BigWorld.callback(0.0, _aih_tick)
		BigWorld.callback(0.0, _aih_tick)

		# Patch SniperCamera.__cameraUpdate to sync camera source position every frame
		try:
			import AvatarInputHandler.cameras as _cams
			_orig_cam_update = _cams.SniperCamera._SniperCamera__cameraUpdate
			_mv_ref = mock_veh
			_vm_ref = veh_matrix
			def _patched_cam_update(cam_self, *a, **kw):
				_orig_cam_update(cam_self, *a, **kw)
				try:
					cam = getattr(cam_self, '_SniperCamera__cam', None)
					if cam is not None and hasattr(cam, 'source'):
						mp = Math.WGTranslationOnlyMP()
						mp.source = _vm_ref
						cam.source = mp
				except Exception:
					pass
			_cams.SniperCamera._SniperCamera__cameraUpdate = _patched_cam_update
			LOG_DEBUG('OfflineBattle.SniperCamera.__cameraUpdate patched')
		except Exception:
			LOG_CURRENT_EXCEPTION()

		# Patch control_modes and cameras ticks to stop gracefully after player is gone
		try:
			import AvatarInputHandler.control_modes as _ctrl
			import AvatarInputHandler.cameras as _cams2
			
			# Patch ArcadeControlMode.__tick
			_orig_ctrl_tick = _ctrl.ArcadeControlMode._ArcadeControlMode__tick
			def _safe_ctrl_tick(self_cm, *a, **kw):
				if BigWorld.player() is None:
					return  # Stop ticking after battle ends
				return _orig_ctrl_tick(self_cm, *a, **kw)
			_ctrl.ArcadeControlMode._ArcadeControlMode__tick = _safe_ctrl_tick
			
			# Patch ArcadeCamera.__cameraUpdate
			if hasattr(_cams2, 'ArcadeCamera') and hasattr(_cams2.ArcadeCamera, '_ArcadeCamera__cameraUpdate'):
				_orig_arc_cam = _cams2.ArcadeCamera._ArcadeCamera__cameraUpdate
				def _safe_arc_cam(self_ac, *a, **kw):
					if BigWorld.player() is None:
						return
					return _orig_arc_cam(self_ac, *a, **kw)
				_cams2.ArcadeCamera._ArcadeCamera__cameraUpdate = _safe_arc_cam
			
			LOG_DEBUG('OfflineBattle.control_modes/cameras ticks patched for safe exit')
		except Exception:
			LOG_CURRENT_EXCEPTION()

		g_offline_aih = AvatarInputHandler.AvatarInputHandler()
		player.inputHandler = g_offline_aih
		try:
			g_offline_aih.start()
		except Exception as e:
			import traceback
			LOG_DEBUG('AvatarInputHandler.start ERROR:', traceback.format_exc())
		
		# After AIH.start(), forcibly redirect camera to our spawn position.
		# AIH may set cam.target to (0,0,0) from a defaulted entity matrix.
		# We override it directly using CursorCamera.
		def _force_camera_to_model():
			try:
				import BigWorld, Math
				cam = BigWorld.camera()
				if cam is not None and hasattr(cam, 'target'):
					# Set cam.target to a translation-only provider tracking veh_matrix.
					# This prevents the camera from turning when the tank hull turns.
					mp = Math.WGTranslationOnlyMP()
					mp.source = veh_matrix
					cam.target = mp
					LOG_DEBUG('OfflineBattle.force_camera: set target to', veh_pos[0], veh_pos[1], veh_pos[2])
				else:
					LOG_DEBUG('OfflineBattle.force_camera: cam=', cam, 'has target=', hasattr(cam, 'target') if cam else False)
			except Exception as e:
				import traceback
				LOG_DEBUG('OfflineBattle.force_camera ERROR:', traceback.format_exc())
		BigWorld.callback(0.1, _force_camera_to_model)
		BigWorld.callback(0.5, _force_camera_to_model)
		BigWorld.callback(1.0, _force_camera_to_model)


		from gui import WindowsManager
		from gui.Scaleform.Waiting import Waiting
		try:
			player = BigWorld.player()
			
			import gui.Scaleform.Battle
			class _FakeAvatarMod(object):
				PlayerAvatar = type(player)
			
			if hasattr(gui.Scaleform.Battle, 'Avatar'):
				gui.Scaleform.Battle.orig_Avatar = gui.Scaleform.Battle.Avatar
			gui.Scaleform.Battle.Avatar = _FakeAvatarMod
			
			if not hasattr(player, 'denunciationsLeft'):
				player.denunciationsLeft = 0
				
			if not hasattr(player, 'onSpaceLoaded'):
				class _DummyEvent(object):
					def __iadd__(self, *a, **k): return self
					def __isub__(self, *a, **k): return self
					def __call__(self, *a, **k): return True
					def isActive(self): return True
				player.onSpaceLoaded = _DummyEvent()
			
			if not hasattr(player, 'playerVehicleID'):
				player.playerVehicleID = 0
			if not hasattr(player, 'isVehicleAlive'):
				player.isVehicleAlive = True
			if not hasattr(player, 'name'):
				player.name = 'Player'
			if not hasattr(player, 'team'):
				player.team = 1
			
			

			


			def _mock_shoot():
				import BigWorld, Math, math, random
				if getattr(BigWorld.player(), '_is_dead', False) is True: return
				try:
					# --- RELOAD LOGIC ---
					if not _gun_state['initialized']: return
					if _gun_state['reloadTime'] > 0: return
					idx = _gun_state.get('shot_index', 0)
					ammo_key = 'ammo_%d' % idx
					if _gun_state.get(ammo_key, 1) <= 0: return
					
					_gun_state[ammo_key] -= 1
					_gun_state['clip'] -= 1
					_gun_state['dispersion'] += _gun_state['base_dispersion'] * _gun_state['after_shot']
					
					if _gun_state['clip'] > 0:
						_gun_state['reloadTime'] = _gun_state['clip_reload']
					else:
						_gun_state['reloadTime'] = _gun_state['reload']
						
					# UPDATE RELOAD UI
					try:
						from gui import WindowsManager
						panel = WindowsManager.g_windowsManager.battleWindow.consumablesPanel
						if panel:
							panel.setShellQuantityInSlot(_gun_state.get('shot_index', 0), _gun_state['ammo_%d' % _gun_state.get('shot_index', 0)], _gun_state['clip'])
							panel.setCoolDownTime(0, _gun_state['reloadTime'])
						aim = getattr(g_offline_aih, 'aim', None)
						if aim:
							aim.setReloading(_gun_state['reloadTime'], None)
							aim.setAmmoStock(_gun_state['ammo_0'], _gun_state['clip'], False)
					except: pass
					
					try:
						player._Avatar__shotWaitingTimerID = None
					except: pass
					
					# --- RAYCAST HIT DETECTION ---
					start_pos, dir_vec = player.gunRotator._VehicleGunRotator__getCurShotPosition()
					dir_vec.normalise()
					
					# --- TRACER ---
					try:
						if g_projectile_mover:
							from items import vehicles
							_our_td = loaded_models.get('td')
							_our_shots = _our_td.gun.get('shots', []) if _our_td else []
							_si = _gun_state.get('shot_index', 0)
							_si = min(_si, len(_our_shots) - 1) if _our_shots else 0
							_shot = _our_shots[_si] if _our_shots else None
							
							if _shot:
								_effectsDescr = vehicles.g_cache.shotEffects[_shot['shell']['effectsIndex']]
								_gravity = _shot['gravity']
								_speed = _shot['speed']
								_vel = dir_vec.scale(_speed)
								import random
								_sid = random.randint(10000, 99999)
								_cam_pos = BigWorld.camera().position if BigWorld.camera() else start_pos
								LOG_DEBUG('Spawning tracer! shotID=%s start_pos=%s vel=%s speed=%s' % (_sid, start_pos, _vel, _speed))
								g_projectile_mover.add(_sid, _effectsDescr, _gravity, start_pos, _vel, start_pos, True, _cam_pos)
					except Exception as e:
						import traceback
						LOG_DEBUG('Tracer spawn error:', traceback.format_exc())
					
					hit = False
					enemy_mock = None
					
					for eid, m_veh in mock_vehicles.iteritems():
						if eid != player.playerVehicleID and getattr(m_veh, 'isAlive', False):
							# Use the actual 3D model position if available, else stored position
							e_pos = getattr(getattr(m_veh, 'model', None), 'position', None) or m_veh.position
							# Also sync stored position with model for future checks
							try: m_veh.position = m_veh.model.position
							except: pass
							v = Math.Vector3(e_pos.x - start_pos.x, e_pos.y - start_pos.y + 1.0, e_pos.z - start_pos.z)
							proj = v.dot(dir_vec)
							if proj > 0:
								dist_sq = v.lengthSquared - proj * proj
								LOG_DEBUG('Raycast check: eid=%d dist_sq=%.1f pos=%s' % (eid, dist_sq, str(e_pos)))
								if dist_sq < 36.0: # ~6m radius (tank sized)
									hit = True
									enemy_mock = m_veh
									break
					
					if hit and enemy_mock:
						# Calculate real damage from gun.shots[i].shell descriptor
						try:
							_td = loaded_models.get('td')
							_gun = _td.gun
							_shots = _gun.get('shots', [])
							_sidx = _gun_state.get('shot_index', 0)
							_sidx = min(_sidx, len(_shots) - 1) if _shots else 0
							_shell = _shots[_sidx].get('shell') if _shots else None
							if _shell and 'damage' in _shell:
								_dmg_data = _shell['damage']
								if hasattr(_dmg_data, '__len__') and len(_dmg_data) >= 1: avg = float(_dmg_data[0])
								else: avg = float(_dmg_data)
								dmg = int(random.uniform(avg * 0.75, avg * 1.25))
								
								# ARMOR PENETRATION LOGIC
								pierce = _shots[_sidx].get('piercingPower', (100.0, 100.0))[0]
								pierce_rng = pierce * random.uniform(0.75, 1.25)
								
								_etd = enemy_mock.typeDescriptor
								# Translate ray to tank local space
								import math
								tank_yaw = enemy_mock.yaw
								
								# Local start pos (relative to tank center)
								dx = start_pos.x - e_pos.x
								dz = start_pos.z - e_pos.z
								dy = start_pos.y - e_pos.y
								
								# Rotate by -yaw
								cos_y = math.cos(-tank_yaw)
								sin_y = math.sin(-tank_yaw)
								local_start_x = dx * cos_y - dz * sin_y
								local_start_z = dx * sin_y + dz * cos_y
								local_start_y = dy
								
								local_dir_x = dir_vec.x * cos_y - dir_vec.z * sin_y
								local_dir_z = dir_vec.x * sin_y + dir_vec.z * cos_y
								local_dir_y = dir_vec.y
								
								# Determine hit part based on height intersection
								# Ray eq: P = start + dir * t
								# We know ray intersects the tank (dist_sq < 36), let's find roughly the Z or X plane intersection
								# Assume tank is a box: X from -1.5 to 1.5, Z from -3.0 to 3.0
								
								t_x = 9999.0
								if abs(local_dir_x) > 0.001:
									# intersection with side planes (X = -1.5 or 1.5)
									plane_x = -1.5 if local_dir_x > 0 else 1.5
									t_x = (plane_x - local_start_x) / local_dir_x
								
								t_z = 9999.0
								if abs(local_dir_z) > 0.001:
									# intersection with front/rear planes (Z = -3.0 or 3.0)
									plane_z = -3.0 if local_dir_z > 0 else 3.0
									t_z = (plane_z - local_start_z) / local_dir_z
								
								# Whichever intersection is further along the ray (smaller positive t), is the face we hit
								# Wait, no. The LARGER t entering the box? Let's just use the ray origin to center vector
								# Actually, simplest AABB: we hit the plane with the largest entry t
								plane_x = 1.5 if local_dir_x < 0 else -1.5
								t_enter_x = (plane_x - local_start_x) / (local_dir_x if local_dir_x != 0 else 0.001)
								
								plane_z = 3.0 if local_dir_z < 0 else -3.0
								t_enter_z = (plane_z - local_start_z) / (local_dir_z if local_dir_z != 0 else 0.001)
								
								hit_t = max(t_enter_x, t_enter_z)
								
								# Calculate Y at intersection
								rel_y = local_start_y + local_dir_y * hit_t
								is_turret = rel_y > 1.4 and len(_etd.turrets) > 0
								hit_part = _etd.turret if is_turret else _etd.hull
								primary_armor = hit_part.get('primaryArmor', (0,0,0))
								
								if hit_t == t_enter_z:
									# Hit front or rear
									is_front = local_start_z > 0
									base_armor = primary_armor[0] if is_front else primary_armor[2]
									angle_cos = abs(local_dir_z)
									intrinsic_slope = 1.6 if is_turret else (1.4 if is_front else 1.0)
								else:
									# Hit side
									base_armor = primary_armor[1]
									angle_cos = abs(local_dir_x)
									intrinsic_slope = 1.3 if is_turret else 1.1
								
								# Minimum angle cos to avoid infinity (85 degrees max)
								angle_cos = max(0.087, angle_cos)
								eff_armor = (base_armor * intrinsic_slope) / angle_cos
								
								LOG_DEBUG('ARMOR: part=%s base=%.1f eff=%.1f pierce=%.1f angle=%.2f' % ('turret' if is_turret else 'hull', base_armor, eff_armor, pierce_rng, angle_cos))
								
								auto_bounce = False
								# 70 degree auto-bounce rule (cos(70) ~ 0.342), except for HE
								if angle_cos < 0.342 and 'HE' not in _shell['name']:
									# Overmatch rule: if caliber > 3 * armor, no auto-bounce
									caliber = _shell.get('caliber', 100) # Default to 100mm if unknown
									if caliber <= base_armor * 3:
										auto_bounce = True
								
								if auto_bounce:
									dmg = 0
									LOG_DEBUG('RICOCHET (Auto-Bounce >70 deg)!')
								elif pierce_rng < eff_armor and 'HE' not in _shell['name']:
									dmg = 0
									LOG_DEBUG('RICOCHET / NON-PENETRATION!')
							else:
								dmg = random.randint(250, 450)
						except Exception as e:
							import traceback
							LOG_DEBUG('Damage calc error:', traceback.format_exc())
							dmg = random.randint(250, 450)
						if dmg > 0:
							enemy_mock.health -= dmg
							LOG_DEBUG('HIT! Damage:', dmg, 'Enemy HP:', enemy_mock.health)
						
						# Update vehicle marker health
						try:
							hp_percent = max(0, int((float(enemy_mock.health) / float(enemy_mock.maxHealth)) * 100.0))
							player.arena.onVehicleStatisticsUpdate(enemy_mock.id)
							from gui import WindowsManager
							bw = getattr(WindowsManager.g_windowsManager, 'battleWindow', None)
							if bw and hasattr(bw, 'vMarkersManager'):
								marker = getattr(enemy_mock, 'marker', None)
								if marker is not None:
									bw.vMarkersManager.onVehicleHealthChanged(marker, enemy_mock.health, 1, 0)
									try: bw.vMarkersManager.showVehicleDamageInfo(marker, dmg, 0, 0, 1)
									except: pass
									LOG_DEBUG('HP updated via marker, HP=%d' % enemy_mock.health)
								else:
									LOG_DEBUG('No marker on enemy_mock!')
							if bw and hasattr(bw, 'minimap'):
								try: bw.minimap.notifyVehicleStop(enemy_mock.id) if enemy_mock.health <= 0 else None
								except: pass
							try: player.showVehicleDamageInfo(enemy_mock.id, 0, 0, dmg)
							except: pass
						except Exception as e:
							LOG_DEBUG('Hit GUI error:', str(e))
						
						if enemy_mock.health <= 0:
							enemy_mock.isAlive = False
							LOG_DEBUG('ENEMY DESTROYED!')
							try:
								WindowsManager.g_windowsManager.battleWindow.vMarkersManager.updateVehicleHealth(enemy_mock.id, 0, 1, 0)
							except: pass
							# --- SWAP TO DESTROYED MODEL ---
							try:
								_dtd = enemy_mock.typeDescriptor
								_d_ch = BigWorld.Model(_dtd.chassis['models']['destroyed'])
								_d_hu = BigWorld.Model(_dtd.hull['models']['destroyed'])
								_d_tu = BigWorld.Model(_dtd.turret['models']['destroyed'])
								_d_gu = BigWorld.Model(_dtd.gun['models']['destroyed'])
								_old_ch = enemy_mock._chassis_model
								_old_pos = _old_ch.position
								_old_yaw = _old_ch.yaw
								_old_ch_ref = _old_ch
								def _swap_destroyed_model(_d_ch=_d_ch, _d_hu=_d_hu, _d_tu=_d_tu, _d_gu=_d_gu, _old_ch_ref=_old_ch_ref, _old_pos=_old_pos, _old_yaw=_old_yaw):
									if not getattr(_d_ch, 'loaded', True) or not getattr(_d_hu, 'loaded', True) or not getattr(_d_tu, 'loaded', True) or not getattr(_d_gu, 'loaded', True):
										BigWorld.callback(0.1, _swap_destroyed_model)
										return
									try: BigWorld.delModel(_old_ch_ref)
									except: pass
									_d_ch.position = _old_pos
									_d_ch.yaw = _old_yaw
									_h_mat = Math.Matrix(); _h_mat.setIdentity()
									_t_mat = Math.Matrix(); _t_mat.setIdentity()
									_g_mat = Math.Matrix(); _g_mat.setIdentity()
									try: _d_ch.node('V').attach(_d_hu)
									except: pass
									try: _d_hu.node('HP_turretJoint', _t_mat).attach(_d_tu)
									except: pass
									try: _d_tu.node('HP_gunJoint', _g_mat).attach(_d_gu)
									except: pass
									_add_model(_d_ch)
									LOG_DEBUG('Destroyed model swapped OK')
								BigWorld.callback(0.0, _swap_destroyed_model)
							except Exception as _de:
								LOG_DEBUG('Destroyed model swap error:', str(_de))
					
					# --- GUNSHOT SOUND & EFFECTS ---
					try:
						if not hasattr(BigWorld.player(), 'soundNotifications'):
							import gui.IngameSoundNotifications as IngameSoundNotifications
							BigWorld.player().soundNotifications = IngameSoundNotifications.IngameSoundNotifications()
							BigWorld.player().soundNotifications.start()
						
						td = loaded_models.get('td')
						# Play visual effects
						if td and hasattr(td, 'gun') and 'effects' in td.gun:
							eff = td.gun['effects']
							from helpers import EffectsList
							m_pos = getattr(mock_veh.model, 'position', start_pos)
							if hasattr(eff, 'trackShoot'):
								eff.trackShoot(BigWorld.player().spaceID, m_pos, dir_vec)
							elif hasattr(eff, 'effectsList'):
								player_eff = EffectsList.EffectsListPlayer(eff.effectsList, eff.keyPoints)
								player_eff.play(mock_veh.model, m_pos, dir_vec)
						# Forcibly play gunshot sound based on caliber
						try:
							caliber = 75
							if td and hasattr(td, 'gun') and 'shots' in td.gun:
								caliber = td.gun['shots'][0]['shell']['caliber']
							
							if caliber > 120:
								sound_event = '/tanks/guns/gun_huge/gun_huge_152mm'
							elif caliber > 100:
								sound_event = '/tanks/guns/gun_large/gun_large_115-152mm'
							elif caliber > 75:
								sound_event = '/tanks/guns/gun_main/gun_main_85-107mm'
							elif caliber > 45:
								sound_event = '/tanks/guns/gun_medium/gun_medium_50-75mm'
							else:
								sound_event = '/tanks/guns/gun_small/gun_small_20-45mm'
							
							root_model = loaded_models.get('chassis') or loaded_models.get('hull') or loaded_models.get('turret') or loaded_models.get('gun')
							if root_model is not None:
								root_model.playSound(sound_event)
						except Exception as e: pass
					except Exception as e: pass
						
					LOG_DEBUG('OfflineBattle: SHOOT HIT LOGIC RUN!')
				except Exception as e:
					import traceback
					LOG_DEBUG('Shoot ERROR:', traceback.format_exc())


			# --- ENEMY CLONE SPAWNER (Key O) ---
			_orig_handleKeyEvent = g_offline_aih.handleKeyEvent
			_spawn_count = [0]
			def _mock_handleKeyEvent(event):
				import BigWorld, Keys, Math
				if event.isKeyDown() and event.key in [Keys.KEY_1, Keys.KEY_2, Keys.KEY_3]:
					try:
						idx = event.key - Keys.KEY_1
						from gui import WindowsManager
						bw = getattr(WindowsManager.g_windowsManager, 'battleWindow', None)
						panel = getattr(bw, 'consumablesPanel', None) if bw else None
						if panel and ('ammo_%d' % idx) in _gun_state:
							if _gun_state.get('shot_index', 0) != idx:
								_gun_state['shot_index'] = idx
								_gun_state['clip'] = min(_gun_state['clip_size'], _gun_state['ammo_%d' % idx])
								_gun_state['reloadTime'] = _gun_state['reload'] # Full reload on switch
								panel.setCurrentShell(idx)
								panel.setShellQuantityInSlot(idx, _gun_state['ammo_%d' % idx], _gun_state['clip'])
					except Exception as e:
						LOG_DEBUG('Key ammo switch error:', str(e))

				if event.isKeyDown() and event.key in (Keys.KEY_O, Keys.KEY_P):
					try:
						player = BigWorld.player()
						start_pos, dir_vec = player.gunRotator._VehicleGunRotator__getCurShotPosition()
						dir_vec.normalise()
						hit = BigWorld.wg_collideSegment(player.spaceID, start_pos, start_pos + dir_vec.scale(500.0), 128)
						target_pos = hit[0] if hit else start_pos + dir_vec.scale(50.0)
						
						# Drop to ground
						ground_hit = BigWorld.wg_collideSegment(player.spaceID, Math.Vector3(target_pos.x, target_pos.y + 100.0, target_pos.z), Math.Vector3(target_pos.x, target_pos.y - 100.0, target_pos.z), 128)
						if ground_hit: target_pos = ground_hit[0]
						
						td = None
						bot_name = 'Bot ' + str(_spawn_count[0])
						if event.key == Keys.KEY_O:
							td = loaded_models.get('td')
							bot_name = 'Clone ' + str(_spawn_count[0])
						elif event.key == Keys.KEY_P:
							try:
								import random
								from items import vehicles
								import nations
								cur_tier = loaded_models['td'].type.level
								candidates = []
								for nation in nations.AVAILABLE_NAMES:
									nationID = nations.INDICES[nation]
									for v in vehicles.g_list.getList(nationID).itervalues():
										if abs(v['level'] - cur_tier) <= 2:
											candidates.append(v['name'])
								LOG_DEBUG('KEY P pressed! cur_tier=%d candidates=%d' % (cur_tier, len(candidates)))
								if candidates:
									chosen = random.choice(candidates)
									td = vehicles.VehicleDescr(typeName=chosen)
									bot_name = chosen.split(':')[-1] + ' ' + str(_spawn_count[0])
							except Exception as e:
								import traceback
								LOG_DEBUG('Random spawn error:', str(e), traceback.format_exc())
								td = loaded_models.get('td')
						
						if not td: return True
						
						e_id = 1000 + _spawn_count[0]
						_spawn_count[0] += 1
						
						# Load visual models
						ch = BigWorld.Model(td.chassis['models']['undamaged'])
						hu = BigWorld.Model(td.hull['models']['undamaged'])
						tu = BigWorld.Model(td.turret['models']['undamaged'])
						gu = BigWorld.Model(td.gun['models']['undamaged'])
						
						def _wait_and_add():
							if not getattr(ch, 'loaded', True) or not getattr(hu, 'loaded', True) or not getattr(tu, 'loaded', True) or not getattr(gu, 'loaded', True):
								BigWorld.callback(0.1, _wait_and_add)
								return
							e_mock = _MockVeh()
							e_mock.id = e_id
							e_mock.position = target_pos
							# Face the player
							import math
							e_mock.yaw = math.atan2(start_pos.x - target_pos.x, start_pos.z - target_pos.z)
							e_mock.health = getattr(td, 'maxHealth', 1000)
							e_mock.maxHealth = e_mock.health
							e_mock.isAlive = True
							e_mock.isStarted = True
							e_mock.publicInfo = {
								'vehicleType': td,
								'name': bot_name,
								'team': 2,
								'isAlive': True,
								'isAvatarReady': True,
								'isTeamKiller': False,
								'accountDBID': 0,
								'clanAbbrev': '',
								'clanDBID': 0,
								'prebattleID': 0,
								'isPrebattleCreator': False,
								'events': {}
							}
							ch.position = e_mock.position
							ch.yaw = e_mock.yaw
							_add_model(ch)
							h_mat = Math.Matrix(); h_mat.setIdentity()
							t_mat = Math.Matrix(); t_mat.setIdentity()
							g_mat = Math.Matrix(); g_mat.setIdentity()
							ch.node('V').attach(hu)
							hu.node('HP_turretJoint', t_mat).attach(tu)
							tu.node('HP_gunJoint', g_mat).attach(gu)
							e_mock.model = ch
							e_mock.typeDescriptor = td
							e_mock._chassis_model = ch
							e_mock._hull_model = hu
							e_mock._turret_model = tu
							e_mock._gun_model = gu
							e_mock._t_mat = t_mat
							class FakeEnemyAppearance(object):
								def __init__(self):
									from Event import Event
									self.onModelChanged = Event()
								def changeVisibility(self, *a, **kw): pass
								def showDamageFromShot(self, *a, **kw): pass
								def showDamageFromExplosion(self, *a, **kw): pass
							e_mock.appearance = FakeEnemyAppearance()
							mock_vehicles[e_id] = e_mock
							import weakref
							e_mock.proxy = weakref.proxy(e_mock)
							
							from gui import WindowsManager
							player.arena.vehicles[e_id] = e_mock.publicInfo
							try:
								player.arena.onVehicleAdded(e_id)
							except: pass
							
							try:
								if hasattr(WindowsManager.g_windowsManager.battleWindow, 'vMarkersManager'):
									e_mock.marker = WindowsManager.g_windowsManager.battleWindow.vMarkersManager.createMarker(e_mock.proxy)
								
								minimap = WindowsManager.g_windowsManager.battleWindow.minimap
								if minimap:
									minimap.notifyVehicleStart(e_mock.id)
							except Exception as e:
								LOG_DEBUG('GUI Add error:', str(e))
							LOG_DEBUG('Enemy Clone Spawned at:', target_pos)
						BigWorld.callback(0.1, _wait_and_add)
						return True
					except Exception as e:
						import traceback
						LOG_DEBUG('Clone spawn error:', traceback.format_exc())
				return _orig_handleKeyEvent(event)
			g_offline_aih.handleKeyEvent = _mock_handleKeyEvent
			# -------------------------------------------

			player.shoot = _mock_shoot
			
			from Account import Account
			if not hasattr(Account, 'shoot'):
				Account.shoot = _mock_shoot
			if not hasattr(Account, 'autoAim'):
				Account.autoAim = lambda self, targetID: None
			if not hasattr(Account, 'isGuiVisible'):
				Account.isGuiVisible = True

			if hasattr(player, 'arena'):
				if player.arena.vehicles:
					player.playerVehicleID = player.arena.vehicles.keys()[0]
			
			Waiting.close()
			WindowsManager.g_windowsManager.startBattle()
			WindowsManager.g_windowsManager.showBattleLoading()
			
			if hasattr(player, 'arena'):
				if hasattr(player.arena, 'onVehicleAdded'):
					for vID in player.arena.vehicles.keys():
						player.arena.onVehicleAdded(vID)
				
				def _finish_battle_load():
					try:
						Waiting.close()
						WindowsManager.g_windowsManager.showBattle()
						BigWorld.worldDrawEnabled(True)
						
						import AvatarInputHandler.cameras
						AvatarInputHandler.cameras.SniperCamera._USE_SWINGING = False
						BigWorld.wg_isSniperModeSwingingEnabled = lambda *a, **kw: False
						def _do():
							try:
								from gui import WindowsManager
								from account_helpers.AccountSettings import AccountSettings
								_orig_getSettings = AccountSettings.getSettings
								def _mock_getSettings(name, *a, **kw):
									res = _orig_getSettings(name, *a, **kw)
									if name == 'sniper' or name == 'arcade':
										if res is None: res = {}
										if isinstance(res, dict):
											defaults = {
												'snpCentralTag': {'alpha': 100, 'type': 0},
												'snpNet': {'alpha': 100, 'type': 0},
												'snpReloader': {'alpha': 100, 'type': 0},
												'snpCondition': {'alpha': 100, 'type': 0},
												'snpCassette': {'alpha': 100, 'type': 0},
												'snpGunTag': {'alpha': 100, 'type': 0},
												'snpMixing': {'alpha': 100, 'type': 0},
												'centralTag': {'alpha': 100, 'type': 0},
												'net': {'alpha': 100, 'type': 0},
												'reloader': {'alpha': 100, 'type': 0},
												'condition': {'alpha': 100, 'type': 0},
												'cassette': {'alpha': 100, 'type': 0},
												'gunTag': {'alpha': 100, 'type': 0},
												'mixing': {'alpha': 100, 'type': 0}
											}
											for k, v in defaults.items():
												if k not in res:
													res[k] = v
									return res
								AccountSettings.getSettings = staticmethod(_mock_getSettings)
								
								if hasattr(player.arena, 'onPeriodChange'):
									_prebattle_time = 5.0
									_battle_duration = 600
									
									# Prebattle Countdown
									player.arena.period = 2
									player.arena.periodLength = _prebattle_time
									player.arena.periodEndTime = BigWorld.serverTime() + _prebattle_time
									player.arena.onPeriodChange(2, player.arena.periodEndTime, _prebattle_time, 0)
									
									# Switch to Battle Mode
									def _start_battle():
										try:
											player.arena.period = 3
											player.arena.periodLength = _battle_duration
											player.arena.periodEndTime = BigWorld.serverTime() + _battle_duration
											player.arena.onPeriodChange(3, player.arena.periodEndTime, _battle_duration, 0)
										except Exception as e:
											LOG_DEBUG('OfflineBattle StartBattle error:', e)
									
									BigWorld.callback(_prebattle_time, _start_battle)
									
								if hasattr(player.arena, 'onNewVehicleListReceived'):
									player.arena.onNewVehicleListReceived()
								if hasattr(player.arena, 'onVehicleAdded'):
									for vID in player.arena.vehicles.keys():
										player.arena.onVehicleAdded(vID)
								if hasattr(player.arena, 'onVehicleStatisticsUpdate'):
									for vID in player.arena.vehicles.keys():
										player.arena.onVehicleStatisticsUpdate(vID)
								if hasattr(WindowsManager.g_windowsManager.battleWindow, '_Battle__populateData'):
									WindowsManager.g_windowsManager.battleWindow._Battle__populateData()
									
								if not getattr(player, '_crosshair_init_done', False):
									player._crosshair_init_done = True
									try:
										import AvatarInputHandler.aims
										try:
											AvatarInputHandler.aims.clearState()
											hs = AvatarInputHandler.aims._g_aimState.get('health')
											if hs is not None:
												hs['cur'] = getattr(player.vehicleTypeDescriptor, 'maxHealth', 400)
												hs['max'] = getattr(player.vehicleTypeDescriptor, 'maxHealth', 400)
										except Exception as e:
											LOG_DEBUG('OfflineBattle aims init error:', str(e))
											
										# Mock the startup to avoid crashing on missing Vehicle entity
										g_offline_aih._AvatarInputHandler__isStarted = True
										g_offline_aih._AvatarInputHandler__isGUIVisible = True
										g_offline_aih._AvatarInputHandler__isArenaStarted = True
										for control in g_offline_aih._AvatarInputHandler__ctrls.itervalues():
											try: control.create()
											except Exception as e: LOG_DEBUG('Control create error:', e)
											
											# Pre-warm the gunMarker state so dumpState() doesn't throw KeyError: 'startTime'
											try: control.setReloading(0.0, 0.0)
											except Exception: pass
											
										try:
											g_offline_aih._AvatarInputHandler__isSPG = 'SPG' in td.type.tags
										except Exception:
											g_offline_aih._AvatarInputHandler__isSPG = False

										g_offline_aih.onControlModeChanged('arcade')
										import SoundGroups
										SoundGroups.g_instance.enableArenaSounds(True)
										g_offline_aih.setGUIVisible(True)
										if hasattr(g_offline_aih, 'ctrl'):
											pass
										try:
											import AvatarInputHandler.aims as aims
											if getattr(aims, '_g_aimState', None) is not None:
												aims._g_aimState['reload'] = {'isReloading': False, 'duration': 0.0, 'startTime': None, 'correction': None}
										except Exception:
											pass
										g_offline_aih.ctrl.showGunMarker(True)
										g_offline_aih.ctrl.showGunMarker2(True)
										_force_camera_to_model()
										LOG_DEBUG('OfflineBattle AIH enable SUCCESS')
									except Exception as e:
										import traceback
										LOG_DEBUG('OfflineBattle AIH enable ERROR:', traceback.format_exc())
							except Exception:
								import traceback
								LOG_DEBUG('Do error:', traceback.format_exc())
						
						BigWorld.callback(0.1, _do)
						
					except Exception:
						LOG_CURRENT_EXCEPTION()
				from _constants import CONFIG_OPTIONS
				loading_time = float(CONFIG_OPTIONS.get('loading_screen_time_seconds', 5.0))
				BigWorld.callback(loading_time, _finish_battle_load)

		except Exception:
			LOG_CURRENT_EXCEPTION()
			WindowsManager.g_windowsManager.hideAll()
		LOG_DEBUG('OfflineBattle.camera started')
	except Exception:
		LOG_CURRENT_EXCEPTION()
	player._offline_allow_become_non_player = False
	LOG_DEBUG('OfflineBattle.spawnAvatar.fail', cmdName)

def _step_on_enqueued(player, vehInvID, cmdName):
	try:
		_enable_offline_battle_transition(player)
		ctx = build_offline_battle_context(player, vehInvID)
		player._offhangar_battle_ctx = ctx
		player._offhangar_player_vehicle_id = ctx.get('playerVehicleID', vehInvID)
		player._offhangar_team = 1
		arena = getattr(player, '_offhangar_arena', None)
		if arena is not None:
			arena.vehicles = ctx.get('vehicles', {})
			arena.guiType = 0
			arena.bonusType = 0
			arena.extraData = {'mapName': ctx.get('mapName'), 'mapID': ctx.get('mapID')}
			arena.period = 1
			arena.periodLength = 600
			arena.periodEndTime = BigWorld.serverTime() + 600
			map_name = ctx.get('mapName', '') or ''
			map_id = ctx.get('mapID', 0) or 0
			gameplay = 'ctf'
			real_arena_type = _resolve_real_arena_type(map_id, map_name, gameplay)
			if real_arena_type is not None:
				arena.arenaType = real_arena_type
				LOG_DEBUG('OfflineBattle.arenaType.real', map_name, 'minimap', hasattr(real_arena_type, 'minimap'))
			elif getattr(arena, 'arenaType', None) is not None:
				# Fallback: keep stub, but ensure required attrs exist.
				arena.arenaType.geometryName = map_name
				arena.arenaType.gameplayName = gameplay
				if not hasattr(arena.arenaType, 'minimap'):
					arena.arenaType.minimap = None
				LOG_DEBUG('OfflineBattle.arenaType.stub', map_name)
		queueType = _queue_type_randoms()
		LOG_DEBUG('OfflineBattle.onEnqueued', cmdName, 'queueType', queueType, 'vehInvID', vehInvID)
		onEnqueued = getattr(player, 'onEnqueued', None)
		if callable(onEnqueued):
			onEnqueued(queueType)
		else:
			onEnqueuedRandom = getattr(player, 'onEnqueuedRandom', None)
			if callable(onEnqueuedRandom):
				onEnqueuedRandom()
		if hasattr(player, 'isInRandomQueue'):
			player.isInRandomQueue = True
	except Exception:
		LOG_CURRENT_EXCEPTION()


def _step_on_arena_created(player, cmdName):
	try:
		if player is None:
			return
		if getattr(player, '_offhangar_arena_created_once', False):
			LOG_DEBUG('OfflineBattle.onArenaCreated skip duplicate', cmdName)
			return
		player._offhangar_arena_created_once = True
		LOG_DEBUG('OfflineBattle.onArenaCreated', cmdName)
		onArenaCreated = getattr(player, 'onArenaCreated', None)
		if callable(onArenaCreated):
			onArenaCreated()
		BigWorld.callback(0.05, lambda: _try_spawn_battle_avatar_stub(BigWorld.player(), cmdName))
	except Exception:
		LOG_CURRENT_EXCEPTION()


def _schedule_arena_created_resilient(cmdName, player):
	def _fire():
		if not getattr(player, '_offhangar_arena_created_once', False):
			_step_on_arena_created(player, cmdName)

	from _constants import CONFIG_OPTIONS
	queue_time = float(CONFIG_OPTIONS.get('queue_wait_time_seconds', 4.0))

	import BigWorld
	BigWorld.callback(queue_time, _fire)
	BigWorld.callback(queue_time + 0.03, _fire)
	BigWorld.callback(queue_time + 0.10, _fire)


def schedule_random_battle_flow_after_enqueue(cmd, cmdName, args):
	"""
	Call after RES_SUCCESS was delivered for an enqueue-like command.
	args: tuple from doCmdInt3 (int1, int2, int3) or similar.
	"""
	if not OFFLINE_BATTLE_ENABLED:
		LOG_DEBUG('OfflineBattle.disabled schedule', cmdName, cmd, args)
		return
	player = BigWorld.player()
	if player is not None:
		now = time.time()
		if now - getattr(player, '_offhangar_sched_debounce', 0) < 1.0:
			LOG_DEBUG('OfflineBattle.schedule debounce', cmdName, cmd, args)
			return
		player._offhangar_sched_debounce = now

	int1 = args[0] if args else 0
	# Never treat server-stats traffic as battle (same numeric cmd id can alias in AccountCommands index).
	if cmdName and ('SERVER_STATS' in cmdName or 'REQ_SERVER_STATS' in cmdName):
		if int1 == 0 and (len(args) < 2 or args[1] == 0) and (len(args) < 3 or args[2] == 0):
			LOG_DEBUG('OfflineBattle.skip stats-shaped packet', cmdName, cmd, args)
			return

	def _run():
		player = BigWorld.player()
		if player is None or not getattr(player, 'isOffline', False):
			return
		player._offhangar_arena_created_once = False
		vehInvID = int1
		if vehInvID == 0 and cmdName and 'ENQUEUE' in cmdName:
			vehInvID = _resolve_vehicle_inv_id(player, 0)
		if not vehInvID:
			LOG_DEBUG('OfflineBattle.skip no vehInvID', cmdName, cmd, args)
			return
		_step_on_enqueued(player, vehInvID, cmdName)
		_schedule_arena_created_resilient(cmdName, player)

	# Run after the current frame so onCmdResponse callbacks finish first.
	BigWorld.callback(0.05, _run)


def start_offline_random_from_hangar(player, vehInvID):
	import traceback
	LOG_DEBUG('OfflineBattle.start_offline_random_from_hangar CALLED', player, getattr(player, 'isOffline', None))
	"""
	0.8.x hangar may spam other doCmd ids before/instead of CMD_ENQUEUE_RANDOM (700).
	When the client calls PlayerAccount.enqueueRandom, short-circuit here so we still
	fire the same BW-side chain as a real matchmaker ack.
	"""
	if not OFFLINE_BATTLE_ENABLED:
		LOG_DEBUG('OfflineBattle.disabled start', vehInvID)
		return
	if player is None:
		LOG_DEBUG('OfflineBattle.disabled start player is None')
		return
	now = time.time()
	if now - getattr(player, '_offline_boot_time', 0.0) < 10.0:
		LOG_DEBUG('OfflineBattle.hook IGNORED AUTO-START inside start_offline')
		return
	last = getattr(player, '_offhangar_battle_last_boot', 0.0)
	if now - last < _BATTLE_BOOT_DEBOUNCE_SEC:
		LOG_DEBUG('OfflineBattle.hook debounce skip', vehInvID)
		return
	player._offhangar_battle_last_boot = now
	cmdName = 'offline.enqueueRandom'

	def _run():
		import BigWorld
		p = BigWorld.player()
		if p is None:
			LOG_DEBUG('OfflineBattle.hook skip p is None')
			return
		p._offhangar_arena_created_once = False
		vid = vehInvID or _resolve_vehicle_inv_id(p, 0)
		if not vid:
			LOG_DEBUG('OfflineBattle.hook skip no vehInvID', vehInvID)
			return
		LOG_DEBUG('OfflineBattle.hook start', cmdName, 'vehInvID', vid)
		_step_on_enqueued(p, vid, cmdName)
		_schedule_arena_created_resilient(cmdName, p)

	import BigWorld
	BigWorld.callback(0.05, _run)
