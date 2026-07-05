import time
import utils
import cPickle
from debug_utils import LOG_DEBUG, LOG_CURRENT_EXCEPTION

g_offline_models = []
g_offline_enemies = []
def _add_model(m):
	global g_offline_models
	g_offline_models.append(m)
	import BigWorld
	BigWorld.addModel(m)

import BigWorld
try:
	from projectilemover import ProjectileMover
	def _safe_calc(self, r0, v0, gravity, isOwnShoot, tracerCameraPos):
		import BigWorld
		end = r0 + v0.scale(2000.0 / v0.length)
		res = BigWorld.wg_collideSegment(BigWorld.player().spaceID, r0, end, 128)
		hitPoint = res[0] if res else end
		return (hitPoint, (hitPoint - r0).length / v0.length)
	ProjectileMover._ProjectileMover__calcTrajectory = _safe_calc
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
						geom_base = geom.split('/')[-1]
						if geom_base == map_name or map_name.endswith(geom_base) or geom_base.endswith(map_name):
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
			if True:
				xml_path = 'scripts/arena_defs/%s.xml' % at.geometryName.split('/')[-1]
				section = ResMgr.openSection(xml_path)
				LOG_DEBUG('OfflineBattle.XML_LOAD:', xml_path, section is not None)
				if section is not None:
					import debug_utils
					debug_utils.LOG_DEBUG('DUMP ARENA DEFS:', section.keys(), section['gameplayTypes/ctf'].keys() if section.has_key('gameplayTypes/ctf') else 'no_ctf')
					if section.has_key('gameplayTypes/ctf'):
						ctf = section['gameplayTypes/ctf']
						for t in ['team1', 'team2']:
							if ctf.has_key('teamSpawnPoints/%s' % t):
								debug_utils.LOG_DEBUG('SPAWN POINTS %s:' % t, ctf['teamSpawnPoints/%s' % t].keys())
								for k, v in ctf['teamSpawnPoints/%s' % t].items():
									debug_utils.LOG_DEBUG(' - ', k, type(v), v.asVector2)
					gp = section['gameplayTypes/ctf']
					if section is not None:
						try:
							with open('C:\\Games\\World_of_Tanks_0.08.02.00.00_EU_0543_SD\\arena_dump_root.txt', 'w') as f_out:
								f_out.write('ROOT keys: ' + str(section.keys()) + '\n')
								for k, v in section.items():
									if k in ['teamSpawnPoints', 'teamBasePositions'] or 'team' in k:
										f_out.write(' - ' + k + ' : ' + str(type(v)) + '\n')
										if hasattr(v, 'keys'):
											f_out.write('    keys: ' + str(v.keys()) + '\n')
						except Exception as e:
							pass
					if gp is not None:
						try:
							with open('C:\\Games\\World_of_Tanks_0.08.02.00.00_EU_0543_SD\\arena_dump_gp.txt', 'w') as f_out:
								f_out.write('ctf keys: ' + str(gp.keys()) + '\n')
								for k, v in gp.items():
									f_out.write(' - ' + k + ' : ' + str(type(v)) + '\n')
									if hasattr(v, 'keys'):
										f_out.write('    keys: ' + str(v.keys()) + '\n')
										for k2, v2 in v.items():
											f_out.write('    - ' + k2 + ' : ' + str(type(v2)) + '\n')
											if hasattr(v2, 'keys'):
												f_out.write('       keys: ' + str(v2.keys()) + '\n')
												for k3, v3 in v2.items():
													f_out.write('       - ' + k3 + ' asVec2:' + str(getattr(v3, 'asVector2', 'none')) + ' asStr:' + str(getattr(v3, 'asString', 'none')) + '\n')
						except Exception as e:
							import debug_utils
							debug_utils.LOG_DEBUG('DUMP ERROR:', e)
						
						global g_offline_bases
						g_offline_bases = {1: [], 2: []}
						import debug_utils
						try:
							bp_node_all = gp['teamBasePositions']
							if bp_node_all is not None:
								debug_utils.LOG_DEBUG('teamBasePositions EXISTS! keys:', bp_node_all.keys())
								for k, v in bp_node_all.items():
									debug_utils.LOG_DEBUG(' - child:', k, v.keys())
						except Exception as e:
							debug_utils.LOG_DEBUG('teamBasePositions error:', e)

						
						for t_id in (1, 2):
							bp_node = gp['teamBasePositions/team%d' % t_id]
							if bp_node is not None:
								items = bp_node.items()
								if items:
									for k, v in items:
										import debug_utils
										debug_utils.LOG_DEBUG('Base node child', t_id, k)
										if v is not None and hasattr(v, 'asVector2'):
											g_offline_bases[t_id].append(Math.Vector3(v.asVector2.x, 0.0, v.asVector2.y))
										elif v is not None and hasattr(v, 'asVector3'):
											g_offline_bases[t_id].append(Math.Vector3(v.asVector3.x, 0.0, v.asVector3.z))
								else:
									import gui.mods.offhangar.logging as __offlog
									__offlog.LOG_DEBUG('LOUD: Base node DIRECT', t_id)
									if hasattr(bp_node, 'asVector2'):
										g_offline_bases[t_id].append(Math.Vector3(bp_node.asVector2.x, 0.0, bp_node.asVector2.y))
									elif hasattr(bp_node, 'asVector3'):
										g_offline_bases[t_id].append(Math.Vector3(bp_node.asVector3.x, 0.0, bp_node.asVector3.z))
									elif hasattr(bp_node, 'asString'):
										try:
											parts = bp_node.asString.split()
											g_offline_bases[t_id].append(Math.Vector3(float(parts[0]), 0.0, float(parts[1])))
										except Exception as e:
											pass
							import gui.mods.offhangar.logging as __offlog
							__offlog.LOG_DEBUG('LOUD: g_offline_bases is now:', g_offline_bases)
						
						import debug_utils
						debug_utils.LOG_DEBUG('Parsed bases:', g_offline_bases)
						
						sp = gp['teamSpawnPoints/team1']
						bp = gp['teamBasePositions/team1']
						
						# PRIORITY: Base Position first! This prevents spawning inside/on top of buildings 
						# at map edges (Spawn points are often behind base and hit testers might hit roofs)
						_found_spawn = False
						if bp is not None:
							import debug_utils
							debug_utils.LOG_DEBUG("DUMPING ALL SPAWNS FOR", map_name)
							if sp is not None:
								for key, val in sp.items():
									vec2 = getattr(val, 'asVector2', None)
									if vec2 is None:
										try:
											parts = val.asString.split()
											vec2 = Math.Vector2(float(parts[0]), float(parts[1]))
										except: pass
									debug_utils.LOG_DEBUG("SPAWN POINT", key, vec2)
							for key, val in bp.items():
								if 'position' in key or key.isdigit():
									vec2 = getattr(val, 'asVector2', None)
									if vec2 is None:
										try:
											parts = val.asString.split()
											vec2 = Math.Vector2(float(parts[0]), float(parts[1]))
										except: pass
									if vec2 is not None:
										y = 100.0
										try:
											import BigWorld
											hit = BigWorld.wg_collideSegment(player.spaceID, Math.Vector3(vec2.x, 1000.0, vec2.y), Math.Vector3(vec2.x, -1000.0, vec2.y), 128)
											if hit is not None:
												y = hit[0].y
										except: pass
										spawn_pos = Math.Vector3(vec2.x, y, vec2.y)
										LOG_DEBUG('OfflineBattle.spawn bp pos:', spawn_pos)
										_found_spawn = True
										break
						
						if not _found_spawn and sp is not None and len(sp.keys()) > 0:
							for key, val in sp.items():
								if val is not None and hasattr(val, 'asVector2'):
									vec2 = val.asVector2
									y = 100.0
									try:
										import BigWorld
										hit = BigWorld.wg_collideSegment(player.spaceID, Math.Vector3(vec2.x, 1000.0, vec2.y), Math.Vector3(vec2.x, -1000.0, vec2.y), 128)
										if hit is not None:
											y = hit[0].y
									except: pass
									spawn_pos = Math.Vector3(vec2.x, y, vec2.y)
									LOG_DEBUG('OfflineBattle.spawn pos:', spawn_pos)
									break
						
						# HACK: Hardcoded safe spawns for known problematic maps where roofs/buildings are hit
						import math
						try:
							if map_name == '04_himmelsdorf':
								spawn_pos = Math.Vector3(17.1, 10.0, 300.0) # North plaza (Team 2 base) is very safe and open
								spawn_dir = Math.Vector3(0, 0, math.radians(180)) # face south
								import BigWorld
								hit = BigWorld.wg_collideSegment(player.spaceID, Math.Vector3(spawn_pos.x, 1000.0, spawn_pos.z), Math.Vector3(spawn_pos.x, -1000.0, spawn_pos.z), 128)
								if hit is not None: spawn_pos.y = hit[0].y
								LOG_DEBUG('OfflineBattle.spawn HARDCODED pos:', spawn_pos)
						except Exception as e: pass
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
					return turret_matrix_local
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
		loaded_models['chassis_mp'] = chassis_mp
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
								
								# Apply Camouflage and Emblems
								try:
									import items.vehicles as iv
									cust = iv.g_cache.customization(td.type.id[0])
									camo_kind = getattr(player.arena.arenaType, 'vehicleCamouflageKind', 0) if hasattr(player, 'arena') and hasattr(player.arena, 'arenaType') else 0
									camo_params = td.camouflages[camo_kind] if hasattr(td, 'camouflages') and len(td.camouflages) > camo_kind else None
									LOG_DEBUG('OfflineBattle.customization:', 'kind', camo_kind, 'params', camo_params)
									if camo_params is not None and camo_params[0] is not None:
										camo = cust['camouflages'].get(camo_params[0]) if cust else None
										if camo is not None:
											tex = camo['texture']
											colors = camo['colors']
											defaultTiling = camo['tiling'].get(td.type.compactDescr)
											weights = Math.Vector4((colors[0]>>24)/255.0, (colors[1]>>24)/255.0, (colors[2]>>24)/255.0, (colors[3]>>24)/255.0)
											for p_name, p_mdl in [('chassis', chassis), ('hull', hull), ('turret', turret), ('gun', gun)]:
												if p_mdl is not None:
													excl = td.type.camouflageExclusionMask
													tiling = defaultTiling
													if tiling is None: tiling = td.type.camouflageTiling
													p_desc = getattr(td, p_name, None)
													if p_desc is not None:
														coeff = p_desc.get('camouflageTiling')
														if coeff is not None and tiling is not None:
															tiling = (tiling[0]*coeff[0], tiling[1]*coeff[1], tiling[2]*coeff[2], tiling[3]*coeff[3])
														if 'camouflageExclusionMask' in p_desc:
															excl = p_desc['camouflageExclusionMask']
													if excl != '' and tex != '':
														

														fashion = getattr(p_mdl, 'wg_baseFashion', None)
														if fashion is None: fashion = p_mdl.wg_baseFashion = BigWorld.WGBaseFashion()
														fashion.setCamouflage(tex, excl, tiling, colors[0], colors[1], colors[2], colors[3], weights)
									
									import VehicleStickers
									emblemPositions = (
										('hull', hull, td.hull['emblemSlots']),
										('gun' if td.turret['showEmblemsOnGun'] else 'turret', gun if td.turret['showEmblemsOnGun'] else turret, td.turret['emblemSlots']),
										('turret' if td.turret['showEmblemsOnGun'] else 'gun', turret if td.turret['showEmblemsOnGun'] else gun, [])
									)
									if not hasattr(player, '_offhangar_stickers'): player._offhangar_stickers = []
									for cName, p_mdl, slots in emblemPositions:
										if p_mdl is not None:
											stickers = VehicleStickers.VehicleStickers(td, slots, cName == 'hull', None)
											try:
												stickers.attachStickers(p_mdl, p_mdl.node(''), False)
											except Exception:
												stickers.attachStickers(p_mdl, p_mdl.root, False)
											player._offhangar_stickers.append(stickers)
								except Exception as e:
									import traceback
									import traceback

									LOG_DEBUG('OfflineBattle.customization error:', str(e), traceback.format_exc())

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

								try:
									import VehicleStickers
									_nodes = loaded_models['sticker_nodes'] = {
										'hull': chassis.node('V') if chassis else hull.node(''),
										'turret': hull.node('HP_turretJoint', turret_mat) if hull else turret.node(''),
										'gun': turret.node('HP_gunJoint', gun_mat) if turret else gun.node('')
									}
									_emblemPositions = (
										('hull', hull, td.hull['emblemSlots']),
										('gun' if td.turret['showEmblemsOnGun'] else 'turret', gun if td.turret['showEmblemsOnGun'] else turret, td.turret['emblemSlots']),
										('turret' if td.turret['showEmblemsOnGun'] else 'gun', turret if td.turret['showEmblemsOnGun'] else gun, [])
									)
									if not hasattr(player, '_offhangar_stickers'): player._offhangar_stickers = []
									for cName, p_mdl, slots in _emblemPositions:
										if p_mdl is not None:
											stickers = VehicleStickers.VehicleStickers(td, slots, cName == 'hull', None)
											p_node = _nodes.get(cName)
											if p_node is not None:
												stickers.attachStickers(p_mdl, p_node, False)
												player._offhangar_stickers.append(stickers)
								except Exception as e:
									import traceback
									LOG_DEBUG('OfflineBattle.stickers error:', str(e), traceback.format_exc())
					elif hull is not None:
						hull.position = Math.Vector3(spawn_pos)
						_add_model(hull)
						LOG_DEBUG('OfflineBattle.addModel OK: hull (no chassis)')
					
					root_model = chassis or hull
					ma.models = [root_model]
					ma.compoundModel = root_model
					LOG_DEBUG('OfflineBattle.compoundModel set, attempt:', _add_attempts[0])


					# Engine sounds are now initialized in _step_offline_physics
				
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

		try:
			for hitTester in td.getHitTesters():
				hitTester.loadBspModel()
		except Exception as e:
			LOG_DEBUG("Error loading hitTesters for player:", str(e))

		class _MockVeh(object):
			def __init__(self):
				self.damage_from_player = 0
				self.damage_from_bots = 0
				self.hits_from_player = 0
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
			
			def getComponents(self):
				import Math
				res = []
				m = Math.Matrix()
				m.setIdentity()
				res.append((self.typeDescriptor.chassis, m))
				
				hullOffset = self.typeDescriptor.chassis['hullPosition']
				m = Math.Matrix()
				m.setTranslate(-hullOffset)
				res.append((self.typeDescriptor.hull, m))
				
				if getattr(self, 'isPlayer', False):
					tYaw = turret_matrix_local.yaw
					gPitch = gun_matrix.pitch if 'gun_matrix' in globals() else 0.0
				else:
					tYaw = getattr(self, '_t_mat', m).yaw
					gPitch = getattr(self, '_g_mat', m).pitch
					
				turretMatrix = Math.Matrix()
				turretMatrix.setTranslate(-hullOffset - self.typeDescriptor.hull['turretPositions'][0])
				m = Math.Matrix()
				m.setRotateY(-tYaw)
				turretMatrix.postMultiply(m)
				res.append((self.typeDescriptor.turret, turretMatrix))
				
				gunMatrix = Math.Matrix()
				gunMatrix.setTranslate(-self.typeDescriptor.turret['gunPosition'])
				m = Math.Matrix()
				m.setRotateX(-gPitch)
				gunMatrix.postMultiply(m)
				gunMatrix.preMultiply(turretMatrix)
				res.append((self.typeDescriptor.gun, gunMatrix))
				
				return res

			def collideSegment(self, startPoint, endPoint, skipGun=False):
				import Math
				worldToVehMatrix = Math.Matrix(self.matrix)
				worldToVehMatrix.invert()
				startPoint = worldToVehMatrix.applyPoint(startPoint)
				endPoint = worldToVehMatrix.applyPoint(endPoint)
				res_closest = None
				all_hits = []
				for (compDescr, compMatrix) in self.getComponents():
					if skipGun and compDescr.get('itemTypeName') == 'vehicleGun':
						continue
					if not hasattr(compDescr.get('hitTester'), 'localHitTest'):
						continue
					collisions = compDescr['hitTester'].localHitTest(compMatrix.applyPoint(startPoint), compMatrix.applyPoint(endPoint))
					if collisions is None:
						continue
					for (dist, _, hitAngleCos, matKind) in collisions:
						matInfo = compDescr.get('materials', {}).get(matKind)
						all_hits.append((dist, hitAngleCos, matInfo, compDescr))
						if res_closest is None or res_closest[0] >= dist:
							res_closest = (dist, hitAngleCos, getattr(matInfo, 'armor', 0) if matInfo is not None else 0)
				if res_closest is not None:
					return (res_closest[0], res_closest[1], res_closest[2], all_hits)
				return None

		# Clear persistent data from previous offline battles, BUT keep the player!
		try:
			global G_OFFHANGAR_SHOTS_FIRED
			G_OFFHANGAR_SHOTS_FIRED = 0
			player = BigWorld.player()
			if hasattr(player, 'arena') and player.arena is not None:
				p_id = getattr(player, 'playerVehicleID', -1)
				if hasattr(player.arena, 'vehicles') and type(player.arena.vehicles) is dict:
					p_veh = player.arena.vehicles.get(p_id, None)
					player.arena.vehicles.clear()
					if p_veh is not None:
						player.arena.vehicles[p_id] = p_veh
				if hasattr(player.arena, 'statistics') and type(player.arena.statistics) is dict:
					p_stat = player.arena.statistics.get(p_id, None)
					player.arena.statistics.clear()
					if p_stat is not None:
						# Reset frags to 0 for the new battle!
						if 'frags' in p_stat: p_stat['frags'] = 0
						player.arena.statistics[p_id] = p_stat
		except: pass
		
		mock_veh = _MockVeh()

		mock_vehicles = {getattr(BigWorld.player(), 'playerVehicleID', -1): mock_veh}
		global G_MOCK_VEHICLES
		G_MOCK_VEHICLES = mock_vehicles

		_orig_entity = BigWorld.entity
		def _mock_entity(eid):
			if eid == getattr(BigWorld.player(), 'playerVehicleID', -1) and eid in mock_vehicles:
				return mock_vehicles[eid]
			orig_e = _orig_entity(eid)
			if orig_e is None and eid in mock_vehicles:
				return mock_vehicles[eid]
			return orig_e
		BigWorld.entity = _mock_entity

		player.getVehicleAttached = lambda: mock_veh
		player.getOwnVehicleMatrix = lambda: veh_matrix
		player.getOwnVehiclePosition = lambda: mock_veh.position
		player._offhangar_gui_visible = False
		def _mock_handleKey(key, isDown, mods=0):
			aih = getattr(player, 'inputHandler', None)
			if aih is not None and hasattr(aih, 'handleKeyEvent'):
				try: return aih.handleKeyEvent(key, isDown, mods)
				except: pass
			return False
		player.handleKey = _mock_handleKey
		
		import game
		if not getattr(game, '_offhangar_hooked', False):
			game._offhangar_hooked = True
			orig_game_handleKeyEvent = game.handleKeyEvent
			def _mock_game_handleKeyEvent(event):
				import Keys
				isDown = event.isKeyDown()
				key = event.key
				if key == Keys.KEY_ESCAPE and not isDown:
					player._offhangar_gui_visible = not getattr(player, '_offhangar_gui_visible', False)
					aih = getattr(player, 'inputHandler', None)
					try:
						import BigWorld, GUI
						if player._offhangar_gui_visible:
							BigWorld.setCursor(GUI.mcursor())
							GUI.mcursor().visible = True
							if aih is not None:
								aih._AvatarInputHandler__isStarted = False
						else:
							if aih is not None:
								aih._AvatarInputHandler__isStarted = True
							BigWorld.setCursor(getattr(GUI, 'ccursor', GUI.mcursor)())
					except Exception: pass
				return orig_game_handleKeyEvent(event)
			game.handleKeyEvent = _mock_game_handleKeyEvent
		
		def _leaveArena():
			_battle_finished[0] = True
			try:
				import SoundGroups as _SG
				if getattr(_SG, 'g_instance', None) is not None:
					_SG.g_instance.enableArenaSounds(False)
					_SG.g_instance.enableLobbySounds(True)
			except Exception: pass
			try:
				_aih = getattr(player, 'inputHandler', None)
				if _aih is not None:
					try: _aih._AvatarInputHandler__isStarted = False
					except: pass
					for _cm in getattr(_aih, '_AvatarInputHandler__ctrls', {}).values():
						try: _cm.destroy()
						except: pass
					player.inputHandler = None
			except Exception: pass

			try:
				from gui import WindowsManager
				if hasattr(WindowsManager.g_windowsManager, 'destroyBattle'):
					WindowsManager.g_windowsManager.destroyBattle()
				else:
					WindowsManager.g_windowsManager.hideAll()
				if hasattr(WindowsManager.g_windowsManager, 'showLobby'):
					WindowsManager.g_windowsManager.showLobby()
			except Exception: pass

			try:
				import BigWorld
				BigWorld.camera(None)
				BigWorld.worldDrawEnabled(True)
			except: pass

			try:
				from gui.Scaleform.utils.HangarSpace import g_hangarSpace
				if g_hangarSpace is not None:
					try: g_hangarSpace.destroy()
					except Exception: pass
					
					# Prevent showLobby from destroying the space
					def _mock_refreshSpace(self, isPremium):
						pass
					g_hangarSpace.__class__.refreshSpace = _mock_refreshSpace
					
					# Force premium
					def _mock_getSpacePath(self, isPremium):
						return self._HangarSpace__space.getDefSpacePath(True)
					g_hangarSpace.__class__._HangarSpace__getSpacePath = _mock_getSpacePath
					
					# Init manually
					g_hangarSpace.init(True)
			except Exception: pass


			try:
				global g_offline_models
				for m in list(g_offline_models):
					try: BigWorld.delModel(m)
					except Exception: pass
				g_offline_models = []
			except Exception: pass
			try:
				import gui.mods.offhangar._constants as _c
				for _e in BigWorld.entities.values():
					if _e.__class__.__name__ in ('PlayerAccount', 'Account'):
						_e._offline_allow_become_non_player = True
						if hasattr(_e, '_offhangar_orig_stats') and _e._offhangar_orig_stats is not None:
							_e.stats = _e._offhangar_orig_stats
						try: _e.showGUI(_c.OFFLINE_GUI_CTX)
						except Exception: pass
			except Exception: pass
			
		player.leaveArena = _leaveArena
		
		def _setGUIVisible(visible):
			aih = getattr(player, 'inputHandler', None)
			if aih is not None:
				try: aih._AvatarInputHandler__isGUIVisible = visible
				except: pass
				if hasattr(aih, 'setGUIVisible'):
					try: aih.setGUIVisible(visible)
					except: pass
		player.setGUIVisible = _setGUIVisible
		
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
					from projectile_trajectory import getShotAngles
					descr = BigWorld.player().vehicleTypeDescriptor
					speed = descr.shot['speed']
					gravity = descr.shot['gravity']
					mat = BigWorld.player().getOwnVehicleMatrix()
					
					# Get exact required gun elevation angle to hit targetPos
					try:
						(shotTurretYaw, shotGunPitch) = getShotAngles(descr, mat, (0, 0), targetPos)
					except Exception:
						shotTurretYaw, shotGunPitch = getattr(self, '_turret_yaw', 0.0), getattr(self, '_gun_pitch', 0.0)
					
					# Clamp to limits so trajectory doesn't draw where gun can't reach
					import math
					try:
						pl = descr.gun['pitchLimits']
						from gun_rotation_shared import calcPitchLimitsFromDesc
						limits = calcPitchLimitsFromDesc(shotTurretYaw, pl)
						if shotGunPitch < limits[0]: shotGunPitch = limits[0]
						elif shotGunPitch > limits[1]: shotGunPitch = limits[1]
					except: pass
					
					try:
						yl = descr.gun.get('turretYawLimits', None)
						if yl is None and descr.turret is not None:
							yl = descr.turret.get('yawLimits', None)
						if yl is not None:
							min_yaw = float(yl[0])
							max_yaw = float(yl[1])
							if abs(min_yaw) > 10.0:
								min_yaw = math.radians(min_yaw)
								max_yaw = math.radians(max_yaw)
							if shotTurretYaw < min_yaw: shotTurretYaw = min_yaw
							elif shotTurretYaw > max_yaw: shotTurretYaw = max_yaw
					except: pass
					
					# Calculate actual world space gun position and velocity vector
					turretOffs = descr.hull['turretPositions'][0] + descr.chassis['hullPosition']
					gunOffs = descr.turret['gunPosition']
					turretWorldMatrix = Math.Matrix()
					turretWorldMatrix.setRotateY(shotTurretYaw)
					turretWorldMatrix.translation = turretOffs
					turretWorldMatrix.postMultiply(mat)
					position = turretWorldMatrix.applyPoint(gunOffs)
					gunWorldMatrix = Math.Matrix()
					gunWorldMatrix.setRotateX(shotGunPitch)
					gunWorldMatrix.postMultiply(turretWorldMatrix)
					vector = gunWorldMatrix.applyVector(Math.Vector3(0, 0, speed))
					
					return (position, vector, Math.Vector3(0, -gravity, 0))
				except Exception as e:
					LOG_DEBUG('OfflineBattle getShotParams ERROR:', str(e))
					# fallback
					try:
						speed = BigWorld.player().vehicleTypeDescriptor.shot['speed']
						gravity = BigWorld.player().vehicleTypeDescriptor.shot['gravity']
					except:
						speed, gravity = 250.0, 9.81
					if hasattr(self, '_gun_pos') and hasattr(self, '_gun_dir'):
						return (self._gun_pos, self._gun_dir.scale(speed), Math.Vector3(0, -gravity, 0))
					startPos = BigWorld.player().getOwnVehiclePosition()
					startPos.y += 2.0
					v0 = BigWorld.camera().direction
					return (startPos, v0.scale(speed), Math.Vector3(0, -gravity, 0))
			def _VehicleGunRotator__getCurShotPosition(self):
				import BigWorld, Math
				try:
					speed = BigWorld.player().vehicleTypeDescriptor.shot['speed']
				except:
					speed = 250.0
				if hasattr(self, '_gun_pos') and hasattr(self, '_gun_dir'):
					return (self._gun_pos, self._gun_dir.scale(speed))
				startPos = BigWorld.player().getOwnVehiclePosition()
				startPos.y += 2.0
				v0 = BigWorld.camera().direction
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
		turret_matrix_local = Math.Matrix()

		# Read turret/gun rotation limits from vehicle descriptor
		_turret_rot_speed = 0.03  # rad per tick default
		_gun_min_pitch    = -0.35  # ~-20 deg (ELEVATION - UP) default
		_gun_max_pitch    =  0.15  # ~+8.6 deg (DEPRESSION - DOWN) default
		_gun_min_yaw      = -3.14159
		_gun_max_yaw      =  3.14159
		try:
			if td is not None:
				rot = td.turret.get('rotationSpeed', None)
				if rot is not None:
					_turret_rot_speed = float(rot) * 0.02  # per 20ms tick
				pl = td.gun.get('pitchLimits', None)
				if pl is not None:
					try:
						import math as _math
						if isinstance(pl, dict):
							mn = pl.get('minPitch', pl.get('minAngle', None))
							mx = pl.get('maxPitch', pl.get('maxAngle', None))
							if mn is not None: _gun_min_pitch = _math.radians(float(mn))
							if mx is not None: _gun_max_pitch = _math.radians(float(mx))
						elif isinstance(pl, (list, tuple)):
							# In WoT, pitchLimits is usually a tuple of (minPitch, maxPitch)
							# where each is a piecewise linear function: ( [yaw_angles], [pitch_angles] )
							# Example: ( ([0, 3.14], [-0.349, -0.349]), ([0, 3.14], [0.139, 0.139]) )
							if len(pl) >= 2:
								# maxPitch (depression) is usually pl[1], minPitch (elevation) is pl[0]
								# Let's extract the first value of the pitch array
								min_p = pl[0]
								max_p = pl[1]
								if isinstance(min_p, (list, tuple)) and len(min_p) >= 2:
									_gun_min_pitch = float(min_p[1][0])
								if isinstance(max_p, (list, tuple)) and len(max_p) >= 2:
									_gun_max_pitch = float(max_p[1][0])
					except Exception as pe:
						LOG_DEBUG('OfflineBattle pitch parsing error:', str(pe))
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
		
		global g_base_capture
		g_base_capture = {1: {'points': 0}, 2: {'points': 0}}
		
		global g_capture_tick_ref
		def trigger_battle_results(winnerTeam=1):
			import BigWorld
			player = BigWorld.player()
			if player is None: return
			try:
				from gui.SystemMessages import SM_TYPE, pushMessage
				pushMessage('Offline battle finished. Returning to Hangar...'.encode('utf-8'), SM_TYPE.Information)
			except Exception as e: pass
			
			try:
				import MusicController
				if hasattr(MusicController, 'g_musicController') and MusicController.g_musicController:
					_mc = MusicController.g_musicController
					try: _mc.stop()
					except: pass
					evt = None
					p_team = getattr(player, 'team', 1)
					if winnerTeam == p_team:
						evt = getattr(MusicController, 'MUSIC_EVENT_COMBAT_VICTORY', getattr(MusicController, 'MUSIC_EVENT_VICTORY', 'music_victory'))
					elif winnerTeam != 0:
						evt = getattr(MusicController, 'MUSIC_EVENT_COMBAT_LOSE', getattr(MusicController, 'MUSIC_EVENT_LOSE', 'music_lose'))
					else:
						evt = getattr(MusicController, 'MUSIC_EVENT_COMBAT_DRAW', getattr(MusicController, 'MUSIC_EVENT_DRAW', 'music_draw'))
					try: _mc.play(evt)
					except: pass
			except Exception as e: pass
			
			try:
				import battle_results_shared
				mock_arena_id = 999
				
				v_id = getattr(player, 'playerVehicleID', 1)
				p_max_health = getattr(getattr(player, 'vehicleTypeDescriptor', None), 'maxHealth', 1000)
				p_health = getattr(getattr(player, 'vehicle', None), 'health', p_max_health)
				
				_player_mock = globals().get('G_MOCK_VEHICLES', {}).get(getattr(player, 'playerVehicleID', -1))
				_p_killer_id = getattr(_player_mock, 'last_killer_id', 255) if p_health <= 0 else 0
				
				p_team = getattr(player, 'team', 1)
				p_dbid = getattr(player, 'databaseID', 1)
				p_name = getattr(player, 'name', 'Player')
				p_cd = getattr(getattr(getattr(player, 'vehicleTypeDescriptor', None), 'type', None), 'compactDescr', 0)
				
				players_dict = {p_dbid: {'name': p_name, 'clanDBID': 0, 'clanAbbrev': '', 'prebattleID': 0, 'team': p_team, 'igrType': 0}}
				vehicles_dict = {v_id: {'health': p_health, 'credits': 10000, 'xp': 1000, 'shots': 10, 'hits': 8, 'he_hits': 0, 'pierced': 8, 'damageDealt': 0, 'damageAssisted': 0, 'damageReceived': max(0, p_max_health - p_health), 'shotsReceived': 0, 'spotted': 0, 'damaged': 0, 'kills': 0, 'tdamageDealt': 0, 'tkills': 0, 'isTeamKiller': False, 'capturePoints': 0, 'droppedCapturePoints': 0, 'mileage': 100, 'lifeTime': 300, 'killerID': _p_killer_id, 'achievements': [], 'repair': 0, 'freeXP': 50, 'details': {}, 'accountDBID': p_dbid, 'team': p_team, 'typeCompDescr': p_cd, 'gold': 0}}
				
				for vid, vinfo in getattr(player.arena, 'vehicles', {}).items():
					if vid == v_id: continue
					bot_team = vinfo.get('team', 2)
					bot_name = vinfo.get('name', 'Bot')
					bot_dbid = vid
					td = vinfo.get('vehicleType', None)
					td_type = getattr(td, 'type', None)
					bot_cd = getattr(td_type, 'compactDescr', 0)
					
					players_dict[bot_dbid] = {'name': bot_name, 'clanDBID': 0, 'clanAbbrev': '', 'prebattleID': 0, 'team': bot_team, 'igrType': 0}
					
					is_killed = not vinfo.get('isAlive', True)
					bot_hp = getattr(td, 'maxHealth', 1000)
					if is_killed: bot_hp = 0
					
					vehicles_dict[vid] = {'health': bot_hp, 'credits': 0, 'xp': 0, 'shots': 0, 'hits': 0, 'he_hits': 0, 'pierced': 0, 'damageDealt': 0, 'damageAssisted': 0, 'damageReceived': getattr(td, 'maxHealth', 1000) - bot_hp, 'shotsReceived': 0, 'spotted': 0, 'damaged': 0, 'kills': 0, 'tdamageDealt': 0, 'tkills': 0, 'isTeamKiller': False, 'capturePoints': 0, 'droppedCapturePoints': 0, 'mileage': 10, 'lifeTime': 300, 'killerID': v_id if is_killed else 0, 'achievements': [], 'repair': 0, 'freeXP': 0, 'details': {}, 'accountDBID': bot_dbid, 'team': bot_team, 'typeCompDescr': bot_cd, 'gold': 0}
				
				mock_res = {
					'arenaUniqueID': mock_arena_id,
					'personal': {'health': p_health, 'credits': 10000, 'xp': 1000, 'shots': 10, 'hits': 8, 'he_hits': 0, 'pierced': 8, 'damageDealt': 0, 'damageAssisted': 0, 'damageReceived': 0, 'shotsReceived': 0, 'spotted': 0, 'damaged': 0, 'kills': 0, 'tdamageDealt': 0, 'tkills': 0, 'isTeamKiller': False, 'capturePoints': 0, 'droppedCapturePoints': 0, 'mileage': 100, 'lifeTime': 300, 'killerID': _p_killer_id, 'achievements': [], 'repair': 0, 'freeXP': 50, 'details': {}, 'accountDBID': p_dbid, 'team': p_team, 'typeCompDescr': p_cd, 'gold': 0, 'xpPenalty': 0, 'creditsPenalty': 0, 'creditsContributionIn': 0, 'creditsContributionOut': 0, 'tmenXP': 0, 'eventCredits': 0, 'eventGold': 0, 'eventXP': 0, 'eventFreeXP': 0, 'eventTMenXP': 0, 'autoRepairCost': 0, 'autoLoadCost': (0, 0), 'autoEquipCost': (0, 0), 'isPremium': True, 'premiumXPFactor10': 15, 'premiumCreditsFactor10': 15, 'dailyXPFactor10': 10, 'aogasFactor10': 10, 'markOfMastery': 0, 'dossierPopUps': []},
					'common': {'arenaTypeID': getattr(player.arena, 'arenaTypeID', 1), 'arenaCreateTime': __import__('time').time(), 'winnerTeam': winnerTeam, 'finishReason': 1, 'duration': 300, 'bonusType': 1, 'guiType': 1, 'vehLockMode': 0},
					'players': players_dict,
					'vehicles': vehicles_dict
				}
				
				
				
				try:
					from gui import WindowsManager
					if hasattr(WindowsManager.g_windowsManager, 'showBattleResults'):
						WindowsManager.g_windowsManager.showBattleResults(mock_arena_id)
				except: pass
				
			except Exception as e:
				import traceback
				import gui.mods.offhangar.logging as __offlog
				__offlog.LOG_DEBUG('CRITICAL ERROR IN TRIGGER BATTLE RESULTS:', e)
				__offlog.LOG_DEBUG(traceback.format_exc())
				
			# Now clean up and leave arena!
			# Restore original stats object which was replaced with FakeStats
			if hasattr(player, '_offhangar_orig_stats') and player._offhangar_orig_stats is not None:
				player.stats = player._offhangar_orig_stats
			
			_leaveArena()
			player.onBecomeNonPlayer()
			
			# HACK: Because we triggered onBecomeNonPlayer manually but never call
			# onBecomePlayer to avoid crashing the offline mock state, we must manually
			# re-bind the requester modules and un-ignore them!
			for helper in ('syncData', 'inventory', 'stats', 'trader', 'shop', 'dossierCache', 'battleResultsCache', 'questProgress'):
				h = getattr(player, helper, None)
				if hasattr(h, 'setAccount'):
					try: h.setAccount(player)
					except: pass
				if hasattr(h, 'onAccountBecomePlayer'):
					try: h.onAccountBecomePlayer()
					except: pass

		def _capture_tick():
			import gui.mods.offhangar.logging as __offlog
			__offlog.LOG_DEBUG('LOUD: Capture tick started running!')
			try:
				if _battle_finished[0]: return
				import BigWorld
				player = BigWorld.player()
				if player is None or _battle_finished[0]:
					return
				
				# Get alive vehicles per team
				vehs_by_team = {1: [], 2: []}
				if getattr(player, 'isVehicleAlive', True):
					vehs_by_team[1].append(player) # player is always team 1
					
				_mock_vehicles = globals().get('G_MOCK_VEHICLES', {})
				for e_mock in _mock_vehicles.values():
					if getattr(e_mock, 'isVehicleAlive', True) and getattr(e_mock, '_team', 2) in vehs_by_team:
						vehs_by_team[e_mock._team].append(e_mock)
				
				# Check base distances
				for base_team, bases in g_offline_bases.items():
					if not bases: continue
					
					invading_team = 2 if base_team == 1 else 1
					
					invaders_count = 0
					for invader in vehs_by_team[invading_team]:
						for base_pos in bases:
							import BigWorld
							if invader == BigWorld.player():
								inv_x = veh_pos[0]
								inv_z = veh_pos[2]
							else:
								inv_x = invader.position.x
								inv_z = invader.position.z
							dx = inv_x - base_pos.x
							dz = inv_z - base_pos.z
							import gui.mods.offhangar.logging as __offlog
							__offlog.LOG_DEBUG('LOUD: Distance to base', base_team, 'is', dx*dx + dz*dz, 'pos:', inv_x, inv_z, 'base:', base_pos.x, base_pos.z)
							if dx*dx + dz*dz <= 2500.0: # 50m radius
								invaders_count += 1
								break
					
					defenders_count = 0
					for defender in vehs_by_team[base_team]:
						for base_pos in bases:
							import BigWorld
							if defender == BigWorld.player():
								def_x = veh_pos[0]
								def_z = veh_pos[2]
							else:
								def_x = defender.position.x
								def_z = defender.position.z
							dx = def_x - base_pos.x
							dz = def_z - base_pos.z
							if dx*dx + dz*dz <= 2500.0:
								defenders_count += 1
								break
					
					state = g_base_capture[base_team]
					old_points = state['points']
					
					# Handle transition from PREBATTLE to BATTLE
					if getattr(player.arena, 'period', 0) == 2 and BigWorld.serverTime() >= getattr(player.arena, 'periodEndTime', 0):
						import gui.mods.offhangar.logging as __offlog
						__offlog.LOG_DEBUG('LOUD: TRANSITION TO BATTLE PERIOD')
						player.arena.period = 3
						player.arena.periodLength = 900
						player.arena.periodEndTime = BigWorld.serverTime() + 900
						player.arena.onPeriodChange(3, player.arena.periodEndTime, 900, 0)

					
					import debug_utils
					if state['points'] != old_points or invaders_count > 0:
						debug_utils.LOG_DEBUG('Capture tick: team', base_team, 'invaders:', invaders_count, 'defenders:', defenders_count, 'points:', state['points'], 'serverTime:', BigWorld.serverTime())
					
					if invaders_count > 0 and defenders_count == 0:
						state['points'] = min(100, state['points'] + min(invaders_count, 3))
					elif invaders_count == 0:
						state['points'] = 0
						
					# Removed old hack
						
					if state['points'] != old_points or invaders_count > 0:
						import gui.mods.offhangar.logging as __offlog
						__offlog.LOG_DEBUG('LOUD: PERIOD:', getattr(player.arena, 'period', None), 'SERVERTIME:', BigWorld.serverTime(), 'PERIODENDTIME:', getattr(player.arena, 'periodEndTime', None))
						__offlog.LOG_DEBUG('Capture UI updating points! base:', base_team, 'points:', state['points'], 'invaders:', invaders_count)
						try:
							import gui.Scaleform.Battle
							if not hasattr(gui.Scaleform.Battle.TeamBasesPanel, '_patched_update'):
								orig = gui.Scaleform.Battle.TeamBasesPanel._TeamBasesPanel__onTeamBasePointsUpdate
								def _hook(self, team, baseID, points, capturingStopped):
									import gui.mods.offhangar.logging as __offlog
									__offlog.LOG_DEBUG('LOUD: UI HOOK! team', team, 'base', baseID, 'pts', points, 'stop', capturingStopped)
									try:
										orig(self, team, baseID, points, capturingStopped)
										__offlog.LOG_DEBUG('LOUD: UI HOOK orig executed successfully!')
									except Exception as e:
										__offlog.LOG_DEBUG('LOUD: UI HOOK EXCEPTION:', e)
								gui.Scaleform.Battle.TeamBasesPanel._TeamBasesPanel__onTeamBasePointsUpdate = _hook
								gui.Scaleform.Battle.TeamBasesPanel._patched_update = True
						except Exception as e:
							__offlog.LOG_DEBUG('LOUD: UI HOOK INIT ERROR:', e)
						try:
							player.arena.onTeamBasePointsUpdate(base_team, 0, state['points'], defenders_count > 0)
						except Exception as e:
							__offlog.LOG_DEBUG('LOUD: Capture UI Error:', e)
					
					if state['points'] >= 100:
						try:
							player.arena.onTeamBaseCaptured(1, base_team)
						except: pass
						_battle_finished[0] = True
						
						# Stop the battle!
						try: player.arena.onPeriodChange(4, BigWorld.serverTime() + 5.0, 5.0, 0) # ArenaPeriod.AFTERBATTLE
						except: pass
						
						# Trigger battle results in 5 seconds
						import BigWorld
						BigWorld.callback(5.0, lambda: trigger_battle_results(3 - base_team))
						
			except Exception as e:
				import gui.mods.offhangar.logging as __offlog
				__offlog.LOG_DEBUG('LOUD: Capture Tick Error:', e)
			finally:
				if not _battle_finished[0]:
					BigWorld.callback(1.0, _capture_tick)
					
		g_capture_tick_ref = _capture_tick
		BigWorld.callback(5.0, _capture_tick)
		
		global g_aih_tick_ref
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
				
				import debug_utils
				if not hasattr(player, '_debug_dump_done_6'):
					player._debug_dump_done_6 = True
					debug_utils.LOG_DEBUG('AIH_TICK DUMP AT START!')
					_mock_vehicles = globals().get('G_MOCK_VEHICLES', {})
					debug_utils.LOG_DEBUG('AIH_TICK keys:', _mock_vehicles.keys())
					
				def _get_terrain_ypr(spaceID, pos, yaw, length=5.0, width=3.0):
					import math, BigWorld, Math
					cos_y = math.cos(yaw)
					sin_y = math.sin(yaw)
					
					hl = length / 2.0
					hw = width / 2.0
					
					# 4 body na podvozku
					fx = pos.x + sin_y * hl
					fz = pos.z + cos_y * hl
					bx = pos.x - sin_y * hl
					bz = pos.z - cos_y * hl
					
					rx = pos.x + cos_y * hw
					rz = pos.z - sin_y * hw
					lx = pos.x - cos_y * hw
					lz = pos.z + sin_y * hw
					
					def get_y(x, z):
						try:
							# Raycast starts just 1.5m above current tank position to avoid hitting roofs
							c = BigWorld.wg_collideSegment(spaceID, Math.Vector3(x, pos.y + 1.5, z), Math.Vector3(x, pos.y - 1000.0, z), 128)
							# Ignore hits that are absurdly high (like walls)
							if c and (c[0].y - pos.y) < 1.0: 
								return c[0].y
						except: pass
						return pos.y
					
					fy = get_y(fx, fz)
					by = get_y(bx, bz)
					ry = get_y(rx, rz)
					ly = get_y(lx, lz)
					
					pitch = -math.atan2(fy - by, length)
					roll = math.atan2(ry - ly, width)
					
					# Pitch and roll limits to prevent insane flips (max ~45 deg)
					pitch = max(-0.8, min(0.8, pitch))
					roll = max(-0.8, min(0.8, roll))
					
					return (yaw, pitch, roll)
					
				def _try_destroy_destructible(spaceID, matInfo, yaw, vel):
					import AreaDestructibles, BigWorld, constants
					try:
						if not hasattr(AreaDestructibles, 'g_destructiblesManager') or not AreaDestructibles.g_destructiblesManager:
							return False
							
						hitPt, surfNormal, chunkID, itemIndex, matKind, fname = matInfo
						LOG_DEBUG('Destr hit:', matKind, fname)
						
						if matKind < constants.DESTRUCTIBLE_MATKIND.MIN or matKind > constants.DESTRUCTIBLE_MATKIND.MAX:
							LOG_DEBUG('Destr failed: invalid matKind', matKind)
							return False
							
						desc = AreaDestructibles.g_cache.getDescByFilename(fname)
						if not desc:
							LOG_DEBUG('Destr failed: no desc for fname', fname)
							return False
						
						ctrl = AreaDestructibles.g_destructiblesManager.getController(chunkID)
						if not ctrl:
							LOG_DEBUG('Destr failed: still no ctrl for chunkID', chunkID)
							return False
						
						typ = desc['type']
						if ctrl.isDestructibleBroken(itemIndex, matKind, typ): 
							LOG_DEBUG('Destr: already broken')
							return True
							
						destrData = 0
						dmgType = 0
						if typ == AreaDestructibles.DESTR_TYPE_TREE:
							destrData = AreaDestructibles.encodeFallenTree(itemIndex, yaw, 0, max(abs(vel), 5.0))
							dmgType = AreaDestructibles._DAMAGE_TYPE_TREE
						elif typ == AreaDestructibles.DESTR_TYPE_FALLING_ATOM:
							destrData = AreaDestructibles.encodeFallenColumn(itemIndex, yaw, max(abs(vel), 5.0))
							dmgType = AreaDestructibles._DAMAGE_TYPE_COLUMN
						else:
							destrData = AreaDestructibles.encodeDestructibleModule(itemIndex, matKind, False)
							dmgType = AreaDestructibles._DAMAGE_TYPE_MODULE if typ == AreaDestructibles.DESTR_TYPE_STRUCTURE else AreaDestructibles._DAMAGE_TYPE_FRAGILE
							
						AreaDestructibles.g_destructiblesManager.orderDestructibleDestroy(chunkID, dmgType, destrData, True)
						LOG_DEBUG('Destr SUCCESS!', typ)
						return True
					except Exception as e:
						LOG_DEBUG('Destr Exception:', str(e))
					return False
					
				def _check_horizontal_collision(spaceID, pos, yaw, vel, td=None):
					import math, BigWorld, Math
					try:
						hw = 1.5
						hl_front = 3.5
						hl_back = 3.5
						
						if td and hasattr(td, 'hull') and 'hitTester' in td.hull:
							try:
								bbox = td.hull['hitTester'].bbox
								hw = max(abs(bbox[0][0]), abs(bbox[1][0])) - 0.1
								hl_back = abs(bbox[0][2])
								hl_front = abs(bbox[1][2])
							except: pass
							
						back_margin = -2.0 if vel > 0 else 2.0
						front_margin = (hl_front + 2.0) if vel > 0 else -(hl_back + 2.0)
						
						cos_y = math.cos(yaw)
						sin_y = math.sin(yaw)
						
						for offset_x in (-hw, 0, hw):
							sx = pos.x + cos_y * offset_x
							sz = pos.z - sin_y * offset_x
							
							x1 = sx + sin_y * back_margin
							z1 = sz + cos_y * back_margin
							x2 = sx + sin_y * front_margin
							z2 = sz + cos_y * front_margin
							
							# Nezávislý scan na stromy a ploty před tankem
							try:
								seg_start = Math.Vector3(sx, pos.y + 0.5, sz)
								seg_stop = Math.Vector3(x2, pos.y + 0.5, z2)
								matInfo = BigWorld.wg_getMatInfoNearPoint(spaceID, seg_start, seg_stop, seg_stop, lambda *a: False)
								if matInfo:
									if _try_destroy_destructible(spaceID, matInfo, yaw, vel):
										# Pokud jsme rozbili strom/plot, můžeme ignorovat pevnou kolizi, která na něj případně navazuje (nebo i když žádná není)
										pass
							except: pass
							
							# Spodní paprsek pro pevnou geometrii (0.6m nad zemí)
							start_bot = Math.Vector3(x1, pos.y + 0.6, z1)
							end_bot = Math.Vector3(x2, pos.y + 0.6, z2)
							col_bot = BigWorld.wg_collideSegment(spaceID, start_bot, end_bot, 128)
							
							if col_bot is not None:
								d_bot = (col_bot[0] - start_bot).length
								target_len = abs(back_margin) + (hl_front if vel > 0 else hl_back) + 0.2
								if d_bot < target_len:
									# Něco jsme trefili, zkontrolujeme horní paprsek (1.6m nad zemí)
									start_top = Math.Vector3(x1, pos.y + 1.6, z1)
									end_top = Math.Vector3(x2, pos.y + 1.6, z2)
									col_top = BigWorld.wg_collideSegment(spaceID, start_top, end_top, 128)
									
									if col_top is not None:
										d_top = (col_top[0] - start_top).length
										if (d_top - d_bot) < 0.5:
											if _try_destroy_destructible(spaceID, col_bot[0], yaw, vel): pass
											else: return True
									else:
										start_mid = Math.Vector3(x1, pos.y + 1.1, z1)
										end_mid = Math.Vector3(x2, pos.y + 1.1, z2)
										col_mid = BigWorld.wg_collideSegment(spaceID, start_mid, end_mid, 128)
										if col_mid is not None:
											d_mid = (col_mid[0] - start_mid).length
											if (d_mid - d_bot) < 0.25:
												if _try_destroy_destructible(spaceID, col_bot[0], yaw, vel): pass
												else: return True
					except: pass
					return False

				if not _engine_state['init']:
					try:
						td = loaded_models.get('td')
						root_model = loaded_models.get('chassis') or loaded_models.get('hull') or loaded_models.get('turret') or loaded_models.get('gun')
						engine_dict = getattr(td, 'engine', None)
						chassis_dict = getattr(td, 'chassis', None)
						if td and engine_dict and chassis_dict and root_model is not None and root_model.inWorld:
							_engine_state['snd1'] = root_model.playSound(engine_dict['sound'])
							_engine_state['snd2'] = root_model.playSound(chassis_dict['sound'])
							_engine_state['init'] = True
							LOG_DEBUG('OfflineBattle: Engine sounds attached!', engine_dict['sound'], chassis_dict['sound'])
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
					
					# Auto-hull rotation if aiming outside limits
					# Only auto-rotate if not manually steering
					if steer == 0:
						steer = _gun_state.get('auto_steer', 0)
				
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
					power_fraction = min(1.0, (cur_speed / max_speed) + (abs(throttle) * 0.3))
					load = 1.0 + (power_fraction * 2.0) # Map to WoT engine modes (1=idle, 2=mid, 3=high)
					if _engine_state['snd1']:
						p = _engine_state['snd1'].param('load')
						if p: p.value = load
					if _engine_state['snd2']:
						p = _engine_state['snd2'].param('speed')
						if p: p.value = cur_speed / max_speed
				except:
					pass
				# Apply position
				if _veh_velocity[0] != 0.0:
					_p_td = loaded_models.get('td')
					if _check_horizontal_collision(player.spaceID, Math.Vector3(veh_pos[0], veh_pos[1], veh_pos[2]), veh_yaw[0], _veh_velocity[0], _p_td):
						_veh_velocity[0] = 0.0 # Zastavit při nárazu do zdi
					else:
						veh_pos[0] += math.sin(veh_yaw[0]) * _veh_velocity[0] * dt
						veh_pos[2] += math.cos(veh_yaw[0]) * _veh_velocity[0] * dt
				
				# --- Hull Rotation (WoT-style) ---
				turn_dir = steer
				
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
					import debug_utils
					try:
						cam_m_debug = Math.Matrix(BigWorld.camera().matrix)
						debug_utils.LOG_DEBUG('POS CHECK:', cam_m_debug.translation, true_gun_pos, 'DIFF:', cam_m_debug.translation.distTo(true_gun_pos))
					except: pass
					
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
					dy = shot_point.y - last_true_gun_pos.y
					dz = shot_point.z - last_true_gun_pos.z
					dist = math.sqrt(dx*dx + dz*dz)
					
					try:
						if _gun_state.get('rmb_down', False) and not getattr(player, '_autoaim_target', None) and 'locked_local_yaw' in _gun_state:
							local_target_yaw = _gun_state['locked_local_yaw']
							target_pitch = _gun_state['locked_local_pitch']
							target_yaw = veh_yaw[0] + local_target_yaw
						else:
							if getattr(player, '_autoaim_target', None) and getattr(player._autoaim_target, 'health', 0) > 0:
								t_pos = Math.Vector3(player._autoaim_target.position)
								t_pos.y += 1.0
								shot_point = t_pos
							from projectile_trajectory import getShotAngles
							mat = BigWorld.player().getOwnVehicleMatrix()
							tYaw, gPitch = getShotAngles(td, mat, (turret_yaw[0], gun_pitch[0]), shot_point)
							local_target_yaw = tYaw
							target_pitch = gPitch
							target_yaw = veh_yaw[0] + local_target_yaw
					except Exception as e:
						# Fallback k jednoduche trigonometrii (nepresne)
						target_yaw = math.atan2(dx, dz)
						local_target_yaw = target_yaw - veh_yaw[0]
						
						if is_arty:
							try:
								shots = td.gun['shots'] if isinstance(td.gun, dict) else getattr(td.gun, 'shots')
								shot = shots[0]
								v = shot['speed'] if isinstance(shot, dict) else getattr(shot, 'speed')
								g = shot['gravity'] if isinstance(shot, dict) else getattr(shot, 'gravity', 9.81)
								g = abs(g)
								if g < 0.1: g = 9.81
								root = v**4 - g * (g * dist**2 + 2 * dy * v**2)
								if root > 0:
									target_pitch = -math.atan((v**2 - math.sqrt(root)) / (g * dist))
								else:
									target_pitch = -math.pi / 4 # 45 degrees max range fallback
							except Exception as ex:
								target_pitch = math.atan2(-dy, dist) # direct fire fallback
						else:
							target_pitch = math.atan2(-dy, dist)
					
					# Normalize angleses
					while local_target_yaw > math.pi: local_target_yaw -= 2*math.pi
					while local_target_yaw < -math.pi: local_target_yaw += 2*math.pi
					while turret_yaw[0] > math.pi: turret_yaw[0] -= 2*math.pi
					while turret_yaw[0] < -math.pi: turret_yaw[0] += 2*math.pi
					
					_gun_state['auto_steer'] = 0
					if not BigWorld.isKeyDown(Keys.KEY_RIGHTMOUSE):
						if _gun_min_yaw is not None and _gun_max_yaw is not None:
							# Check if aiming outside bounds
							if local_target_yaw < _gun_min_yaw - 0.02: _gun_state['auto_steer'] = -1
							elif local_target_yaw > _gun_max_yaw + 0.02: _gun_state['auto_steer'] = 1
					
					# Clamp to max traverse limits (for SPGs and TDs)
					local_target_yaw = max(_gun_min_yaw, min(_gun_max_yaw, local_target_yaw))
					
					diff_yaw = local_target_yaw - turret_yaw[0]
					if diff_yaw > math.pi: diff_yaw -= 2*math.pi
					if diff_yaw < -math.pi: diff_yaw += 2*math.pi
					
					_gun_state['yaw_penalty'] = abs(diff_yaw) * 0.1
					
					try:
						gun_dir = shot_point - last_true_gun_pos
						if gun_dir.length > 0.001:
							gun_dir.normalise()
							_mock_vehicles = globals().get('G_MOCK_VEHICLES', {})
							
							import debug_utils
							if not hasattr(player, '_debug_dump_done_5'):
								player._debug_dump_done_5 = True
								debug_utils.LOG_DEBUG('AIH_TICK DUMP keys:', _mock_vehicles.keys())
								for _k, _v in _mock_vehicles.items():
									debug_utils.LOG_DEBUG(' - Veh', _k, getattr(_v, '_bot_team', 'N/A'))
							
							closest_bot = None
							min_dist = 9999.0
							for eid, m_veh in _mock_vehicles.iteritems():
								if eid == getattr(player, 'playerVehicleID', -1): continue
								if getattr(m_veh, 'health', 0) <= 0: continue
								b_pos = Math.Vector3(m_veh.position)
								b_vec = b_pos - last_true_gun_pos
								proj_len = b_vec.dot(gun_dir)
								if getattr(m_veh, '_bot_team', None) is not None:
									LOG_DEBUG('REAL_RAYCAST bot %s: proj_len=%.2f, b_pos=(%.1f,%.1f,%.1f), b_vec_len=%.2f' % (eid, proj_len, b_pos.x, b_pos.y, b_pos.z, b_vec.length))
								if proj_len > 0:
									proj_pt = last_true_gun_pos + gun_dir.scale(proj_len)
									dist_to_ray = (b_pos - proj_pt).length
									if getattr(m_veh, '_bot_team', None) is not None:
										LOG_DEBUG('REAL_RAYCAST HIT bot %s: dist_to_ray=%.2f' % (eid, dist_to_ray))
									if dist_to_ray < 2.5:
										if proj_len < min_dist:
											min_dist = proj_len
											closest_bot = m_veh
							prev_bot = getattr(player, '_outlined_bot', None)
							if prev_bot and prev_bot != closest_bot:
								try:
									if hasattr(prev_bot, 'bw_entity') and prev_bot.bw_entity:
										BigWorld.wgDelEdgeDetectEntity(prev_bot.bw_entity)
								except Exception as e:
									pass
								player._outlined_bot = None
							if closest_bot and prev_bot != closest_bot:
								color = 2 if getattr(closest_bot, '_bot_team', 2) == getattr(player, '_offhangar_team', 1) else 1
								try:
									if hasattr(closest_bot, 'bw_entity') and closest_bot.bw_entity:
										BigWorld.wgAddEdgeDetectEntity(closest_bot.bw_entity, color)
										LOG_DEBUG('REAL_RAYCAST OUTLINE APPLIED TO BOT', closest_bot.bw_entity.id)
									else:
										LOG_DEBUG('REAL_RAYCAST bot has no bw_entity!')
								except Exception as e:
									LOG_DEBUG('Outline dummy err:', str(e))
								player._outlined_bot = closest_bot
					except Exception as e:
						import debug_utils
						debug_utils.LOG_DEBUG('Outline error:', str(e))
					
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
				
				# DEBUG CHASSIS KEYS
				try:
					if getattr(mock_veh, '_dbg_keys_logged', None) is None:
						_td_dbg = loaded_models.get('td')
						if _td_dbg and hasattr(_td_dbg, 'chassis'):
							try: LOG_DEBUG('CHASSIS BBOX:', _td_dbg.chassis['hitTester'].bbox)
							except: pass
						import AreaDestructibles, inspect, constants
						if hasattr(AreaDestructibles, 'g_destructiblesManager'):
							if getattr(AreaDestructibles.g_destructiblesManager, 'getSpaceID', lambda: -1)() != BigWorld.player().spaceID:
								AreaDestructibles.g_destructiblesManager.startSpace(BigWorld.player().spaceID)
							try: LOG_DEBUG('DESTRUCTIBLE_MATKIND MIN/MAX:', constants.DESTRUCTIBLE_MATKIND.MIN, constants.DESTRUCTIBLE_MATKIND.MAX)
							except: pass
							try: LOG_DEBUG('BW collide doc:', BigWorld.collide.__doc__)
							except: pass
							try:
								# Log DestructiblesController methods!
								import AreaDestructibles
								if getattr(AreaDestructibles.g_destructiblesManager, 'getSpaceID', lambda: -1)() != BigWorld.player().spaceID:
									AreaDestructibles.g_destructiblesManager.startSpace(BigWorld.player().spaceID)
								chunkID = AreaDestructibles.chunkIDFromPosition(BigWorld.player().position)
								ctrl = AreaDestructibles.g_destructiblesManager.getController(chunkID)
								if ctrl:
									LOG_DEBUG('DestructiblesController dir:', dir(ctrl))
								else:
									LOG_DEBUG('DestructiblesController ctrl is NONE')
							except Exception as e:
								LOG_DEBUG('DestructiblesController EXCEPTION:', str(e))
							try: LOG_DEBUG('encodeDestructibleModule argspec:', inspect.getargspec(AreaDestructibles.encodeDestructibleModule))
							except: pass
							try: LOG_DEBUG('encodeFallenTree argspec:', inspect.getargspec(AreaDestructibles.encodeFallenTree))
							except: pass
							try: LOG_DEBUG('encodeFallenColumn argspec:', inspect.getargspec(AreaDestructibles.encodeFallenColumn))
							except: pass
							try: LOG_DEBUG('wg_getMatInfoNearPoint doc:', BigWorld.wg_getMatInfoNearPoint.__doc__)
							except: pass
							try: LOG_DEBUG('onChunkLoad argspec:', inspect.getargspec(AreaDestructibles.g_destructiblesManager.onChunkLoad))
							except: pass
						mock_veh._dbg_keys_logged = True
				except: pass
				
				# Vypočítat náklon tanku hráče podle terénu
				_p_ypr = _get_terrain_ypr(BigWorld.player().spaceID, mock_veh.position, veh_yaw[0])
				mock_veh.pitch = _p_ypr[1]
				mock_veh.roll = _p_ypr[2]
				
				# Update base matrix IN PLACE so AvatarInputHandler doesn't lose the reference
				mock_veh.matrix.setRotateYPR(_p_ypr)
				mock_veh.matrix.translation = mock_veh.position
				
				if hasattr(mock_veh, 'filter'):
					mock_veh.filter.position = mock_veh.position
					mock_veh.filter.yaw = veh_yaw[0]
					
				# Update camera matrix (needs both translation AND yaw for SniperCamera offsets to work)
				# (Arcade camera strips yaw using WGTranslationOnlyMP later)
				new_m = Math.Matrix()
				new_m.setRotateYPR(_p_ypr)
				new_m.translation = mock_veh.position
				veh_matrix.a = new_m

				# Update chassis matrix (position + yaw) - Servo drives the model
				# Skip if in sniper mode so the tank stays hidden underground
				_in_sniper_now = (hasattr(g_offline_aih, 'ctrl') and
								  g_offline_aih.ctrl.__class__.__name__ == 'SniperControlMode')
				if not _in_sniper_now:
					chassis_new = Math.Matrix()
					chassis_new.setRotateYPR(_p_ypr)
					chassis_new.translation = mock_veh.position
					chassis_mp.a = chassis_new

				# Engine sounds are handled in _step_offline_physics


										
						

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
							
							# Equipment & Crew Modifiers
							has_rammer, has_egld, has_vents, has_vstab, has_rations = False, False, False, False, False
							has_bia, has_snapshot, has_smooth_ride = True, False, False
							
							# Hardcode consumables if none found or to guarantee they exist in offline mode
							_gun_state['consumables'] = [
								{'slot': 3, 'tag': 'repairkit', 'name': 'smallrepairkit', 'icon': '../maps/icons/artefact/smallRepairkit.png', 'used': False},
								{'slot': 4, 'tag': 'medkit', 'name': 'smallmedkit', 'icon': '../maps/icons/artefact/smallMedkit.png', 'used': False},
								{'slot': 5, 'tag': 'extinguisher', 'name': 'handextinguishers', 'icon': '../maps/icons/artefact/handExtinguishers.png', 'used': False}
							]
							
							try:
								from CurrentVehicle import g_currentVehicle
								if g_currentVehicle and hasattr(g_currentVehicle, 'item') and g_currentVehicle.item:
									v_item = g_currentVehicle.item
									
									try:
										import debug_utils
										debug_utils.LOG_DEBUG('DEBUG STATS COMP: td.gun.aimingTime=', getattr(td.gun, 'aimingTime', None), 'v_item.descriptor.gun.aimingTime=', getattr(v_item.descriptor.gun, 'aimingTime', None))
									except: pass
									
									# Parse Equipment
									for dev in getattr(v_item, 'optDevices', []):
										if not dev: continue
										name = getattr(dev, 'name', '') or getattr(getattr(dev, 'descriptor', None), 'name', '') or str(dev)
										name = str(name).lower()
										import debug_utils
										debug_utils.LOG_DEBUG('Parsed Equipment Name:', name)
										if 'rammer' in name: has_rammer = True
										if 'aimdrives' in name: has_egld = True
										if 'ventilation' in name: has_vents = True
										if 'stabilizer' in name: has_vstab = True
									# Parse Consumables from g_currentVehicle if available
									# (We already hardcoded them above, but we can override if needed)
									
									_eqs_list = list(getattr(v_item, 'eqs', []))
									if any(_eqs_list):
										_gun_state['consumables'] = []
									
									for idx, eq in enumerate(_eqs_list):
										if not eq: continue
										name = getattr(eq, 'name', '') or getattr(getattr(eq, 'descriptor', None), 'name', '') or str(eq)
										name = str(name).lower()
										if any(x in name for x in ('ration', 'chocolate', 'cola', 'coffee', 'pudding')): has_rations = True
										icon = getattr(eq, 'icon', None) or getattr(getattr(eq, 'descriptor', None), 'icon', None)
										icon_path = icon[0] if icon and isinstance(icon, tuple) else ''
										if not icon_path:
											if 'medkit' in name: icon_path = '../maps/icons/artefact/smallMedkit.png'
											elif 'repair' in name: icon_path = '../maps/icons/artefact/smallRepairkit.png'
											elif 'extinguisher' in name: icon_path = '../maps/icons/artefact/handExtinguishers.png'
										
										import debug_utils
										debug_utils.LOG_DEBUG('DUMP CONSUMABLE:', name, icon, icon_path)
										tag_name = 'extinguisher' if 'extinguisher' in name else ('medkit' if 'medkit' in name else ('repairkit' if 'repair' in name else ''))
										if tag_name:
											_gun_state['consumables'].append({
												'slot': idx + 3,
												'tag': tag_name,
												'name': name,
												'icon': icon_path,
												'used': False
											})
										
									# Parse Crew Perks
									crew = getattr(v_item, 'crew', [])
									import debug_utils
									debug_utils.LOG_DEBUG('CREW OBJECT IS:', len(crew), crew)
									if not crew: has_bia = False
									for idx, item in enumerate(crew):
										try:
											tman = item[1] if isinstance(item, tuple) and len(item) == 2 else item
											
											if tman is None:
												has_bia = False
												continue
											
											tman_skills = []
											if hasattr(tman, 'skills'):
												for sk in tman.skills:
													name = getattr(sk, 'name', '') or str(sk)
													tman_skills.append(str(name).lower())
											elif hasattr(tman, 'descriptor') and hasattr(tman.descriptor, 'skills'):
												tman_skills = [str(sk).lower() for sk in tman.descriptor.skills]
											
											if 'brotherhood' not in tman_skills: has_bia = False
											if 'smoothturret' in tman_skills or 'snapshot' in tman_skills: has_snapshot = True
											if 'smoothdriving' in tman_skills or 'smoothride' in tman_skills: has_smooth_ride = True
										except Exception as ce:
											import debug_utils
											debug_utils.LOG_DEBUG('Crew member parsing error:', str(ce))
											has_bia = False
							except Exception as e:
								import debug_utils
								debug_utils.LOG_DEBUG('Equipment/Crew parsing error:', str(e))
								has_bia = False
							
							# Calculate crew multiplier (Base 100% crew + Commander 10% bonus)
							crew_skill, commander_skill = 100.0, 100.0
							if has_vents:
								crew_skill += 5.0
								commander_skill += 5.0
							if has_bia:
								crew_skill += 5.0
								commander_skill += 5.0
							if has_rations:
								crew_skill += 10.0
								commander_skill += 10.0
							effective_skill = crew_skill + (commander_skill * 0.1)
							crew_mult = 1.0 / (0.5 + 0.005 * effective_skill)
							
							_gun_state['base_dispersion'] *= crew_mult
							_gun_state['aim_time'] *= crew_mult
							_gun_state['reload'] *= crew_mult
							_gun_state['clip_reload'] *= crew_mult
							
							if has_rammer:
								_gun_state['reload'] *= 0.9
								_gun_state['clip_reload'] *= 0.9
							if has_egld:
								_gun_state['aim_time'] /= 1.1
							_gun_state['has_vstab'] = has_vstab
							_gun_state['has_snapshot'] = has_snapshot
							_gun_state['has_smooth_ride'] = has_smooth_ride
							
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
										try:
											from CurrentVehicle import g_currentVehicle
											v_shells = []
											if g_currentVehicle and g_currentVehicle.item:
												shells = getattr(g_currentVehicle.item, 'shells', [])
												for sh in shells:
													if hasattr(sh, 'count'): v_shells.append(sh.count)
													elif isinstance(sh, tuple) and len(sh) >= 2: v_shells.append(sh[1])
										except:
											v_shells = []
											
										for i, shot in enumerate(shots):
											try: shell = shot['shell']
											except: shell = getattr(shot, 'shell', None)
											try: piercing_val = shot['piercingPower']
											except: piercing_val = getattr(shot, 'piercingPower', 100)
											if isinstance(piercing_val, (tuple, list)): piercing_val = piercing_val[0]
											
											if v_shells and i < len(v_shells):
												qty = v_shells[i]
											else:
												qty = int(ammo_pool * 0.6) if i == 0 else (int(ammo_pool * 0.3) if i == 1 else int(ammo_pool * 0.1))
												if qty == 0 and ammo_pool > 0: qty = 1
											
											_gun_state['ammo_%d' % i] = qty
											panel.addShellSlot(i, qty, _gun_state['clip_size'], _gun_state['clip_size'], shell, piercing_val)
											
										# Find first shell with > 0 ammo
										first_active = 0
										for i in xrange(len(shots)):
											if _gun_state.get('ammo_%d' % i, 0) > 0:
												first_active = i
												break
										_gun_state['shot_index'] = first_active
										
										# Select the first shell as active to show clip UI
										panel.setCurrentShell(first_active)
										panel.setShellQuantityInSlot(first_active, _gun_state['ammo_%d' % first_active], _gun_state['clip'])
									except Exception as ex: LOG_DEBUG('SHELL SLOT FAIL:', str(ex))
									
									try:
										import AvatarInputHandler.aims as aim
										aim.setClipParams(_gun_state['clip_size'], 1)
										aim.setAmmoStock(_gun_state['ammo_%d' % first_active], _gun_state['clip'], False)
										
										# Vynutit reset ukazatele zdraví v GUI!
										from gui import WindowsManager
										bw = getattr(WindowsManager.g_windowsManager, 'battleWindow', None)
										if bw is not None:
											_mh = getattr(td, 'maxHealth', 400)
											if hasattr(bw, 'damagePanel'):
												try: bw.damagePanel._DamagePanel__callFlash('setMaxHealth', [_mh])
												except Exception as e: LOG_DEBUG('CRITICAL ERROR IN K KEY:', e); import traceback; LOG_DEBUG(traceback.format_exc())
												bw.damagePanel.updateHealth(_mh)
											if hasattr(bw, 'vMarkersManager'):
												pass # bw.vMarkersManager.updateVehicleHealth(player.playerVehicleID, _mh, 1, 0)
									except Exception as e: pass
									
									# Add Consumables to UI
									if not _gun_state.get('consumables_added_to_ui'):
										_gun_state['consumables_added_to_ui'] = True
										import debug_utils
										debug_utils.LOG_DEBUG('ADDING CONSUMABLES TO UI:', _gun_state.get('consumables', []))
										
										class FakeEqDescr(object):
											def __init__(self, tag, icon, name):
												self.tags = set([tag])
												self.icon = [icon]
												self.userString = name
												self.description = ''
										
										for cons in _gun_state.get('consumables', []):
											idx = cons['slot']
											tag = cons['tag']
											icon = cons['icon']
											name = cons['name']
											try:
												panel.addEquipmentSlot(idx, 1, FakeEqDescr(tag, icon, name))
											except Exception as e:
												import debug_utils
												debug_utils.LOG_DEBUG('Failed to addEquipmentSlot:', str(e))
									
									_gun_state['GUI_INIT'] = True
									LOG_DEBUG('OfflineBattle: GUI panel initialized!')
							except Exception as e:
								LOG_DEBUG('OfflineBattle GUI Init Error:', str(e))
						cur_time = BigWorld.time()
						if 'last_time' not in _gun_state: _gun_state['last_time'] = cur_time
						dt = cur_time - _gun_state['last_time']
						_gun_state['last_time'] = cur_time
						
						target_disp = _gun_state['base_dispersion']
						
						# Apply native movement/rotation penalties
						try:
							native_disp = player.getOwnVehicleShotDispersionAngle(player.gunRotator.turretRotationSpeed)
							# calculate penalty part alone
							penalty = native_disp[1] - (native_disp[1] / 1.5) # approximate base fallback if needed
							# Wait, native_disp[1] could be evaluated without our crew_mult.
							# We will isolate the penalty mathematically:
							# native base might be larger than our crew-buffed base, so we subtract our base_dispersion.
							# We use max(0, ...) to ensure we don't get negative penalties if native_base was smaller.
							
							raw_penalty = native_disp[1] - _gun_state['base_dispersion']
							if raw_penalty < 0: raw_penalty = 0
							if _gun_state.get('has_vstab', False):
								raw_penalty *= 0.8
							if _gun_state.get('has_snapshot', False):
								raw_penalty *= 0.925
							if _gun_state.get('has_smooth_ride', False):
								raw_penalty *= 0.96
								
							target_disp = _gun_state['base_dispersion'] + raw_penalty
						except:
							try:
								v_speed, r_speed = player.getOwnVehicleSpeeds()
								target_disp += abs(v_speed) * 0.015 + abs(r_speed) * 0.015
							except: pass
						
						penalty = _gun_state.get('yaw_penalty', 0.0)
						target_disp += penalty
						
						if _gun_state['dispersion'] > target_disp:
							import math
							factor = math.exp(-dt * 2.5 / max(_gun_state['aim_time'], 0.1))
							_gun_state['dispersion'] = target_disp + (_gun_state['dispersion'] - target_disp) * factor
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
										shot_idx = _gun_state.get('shot_index', 0)
										panel.setShellQuantityInSlot(shot_idx, _gun_state['ammo_%d' % shot_idx], _gun_state['clip'])
										panel.setCoolDownTime(shot_idx, 0.0)
									aim = getattr(g_offline_aih, 'aim', None)
									if aim:
										aim.setReloading(0.0, None)
										shot_idx = _gun_state.get('shot_index', 0)
										aim.setAmmoStock(_gun_state['ammo_%d' % shot_idx], _gun_state['clip'], True if _gun_state['clip'] == _gun_state['clip_size'] else False)
									
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
						
						gun_dir = gunWorldMatrix.applyToAxis(2)
						gun_dir.normalise()
						
						if 'gun_node_matrix' in loaded_models:
							# Store ONLY the true_gun_pos (pivot). NO rotation.
							# SniperCamera applies its own pitch/yaw from mouse input,
							# and then automatically applies the tank's configured pivotPos.
							_cam_m = Math.Matrix()  # identity = no rotation
							_cam_m.translation = true_gun_pos
							loaded_models['gun_node_matrix'].set(_cam_m)
						
						# Pass gun pos to rotator for Arty/Arcade raycasts
						if hasattr(player, 'gunRotator'):
							player.gunRotator._gun_pos = true_gun_pos
							player.gunRotator._gun_dir = gun_dir
							
						is_arty = False
						try: is_arty = 'SPG' in td.type.tags
						except: pass
						# Calculate exact terrain intersection for the green marker (perfectly simulates server)
						_end_gun = true_gun_pos + gun_dir.scale(10000.0)
						_col_gun = None
						try:
							_col_gun = BigWorld.wg_collideSegment(BigWorld.player().spaceID, true_gun_pos, _end_gun, 128)
						except Exception:
							pass
						gun_target_pos = _col_gun[0] if _col_gun is not None else _end_gun
						
						if hasattr(player, 'gunRotator') and len(player.gunRotator.markerInfo) >= 2:
							mtp = player.gunRotator.markerInfo[0]
							mdir = player.gunRotator.markerInfo[1]
							
							if isinstance(mtp, tuple) and mtp == (0.0, 0.0, 0.0):
								pass # Offline stub, ignore
							elif isinstance(mtp, Math.Vector3) and mtp.lengthSquared == 0.0:
								pass # Offline stub, ignore
							else:
								if isinstance(mtp, tuple):
									gun_target_pos = Math.Vector3(mtp[0], mtp[1], mtp[2])
								else:
									gun_target_pos = mtp
									
								if isinstance(mdir, tuple):
									gun_dir = Math.Vector3(mdir[0], mdir[1], mdir[2])
								else:
									gun_dir = mdir
							
						if _tick_counter[0] % 50 == 0:
							LOG_DEBUG('OfflineBattle.gun: target_pos=', gun_target_pos, 'dir=', gun_dir, 'pos=', true_gun_pos)
							
						# Hide vehicle in sniper mode using model.visible
						if hasattr(g_offline_aih, 'ctrl'):
							is_sniper = g_offline_aih.ctrl.__class__.__name__ == 'SniperControlMode'
							was_sniper = getattr(g_offline_aih, '_was_sniper', None)
							if is_sniper != was_sniper:
								g_offline_aih._was_sniper = is_sniper
								for _part in ('chassis', 'hull', 'turret', 'gun'):
									_mdl = loaded_models.get(_part)
									if _mdl is not None:
										try: _mdl.visible = not is_sniper
										except: pass
								# Tank is hidden via .visible=False, so no need to push underground.
								# Keeping it at real position ensures 3D sounds (engine, gun) remain audible!

						# Calculate perfectly synchronous math_gun_world for raycast
						math_turret_pos = td.chassis['hullPosition'] + td.hull['turretPositions'][0]
						math_gun_world = Math.Matrix(mat).applyPoint(math_turret_pos)
						yaw_mat = Math.Matrix()
						yaw_mat.setRotateY(turret_yaw[0])
						math_gun_world += Math.Matrix(mat).applyVector(yaw_mat.applyVector(td.turret['gunPosition']))

						_end_gun = math_gun_world + gun_dir.scale(10000.0)
						if not is_arty:
							_col_gun = BigWorld.wg_collideSegment(BigWorld.player().spaceID, math_gun_world, _end_gun, 128)
							if _col_gun is not None:
								gun_hit = _col_gun
								dist_to_target = (shot_point - math_gun_world).length
								if gun_hit[1] < dist_to_target:
									dist_to_static = (gun_hit[0] - math_gun_world).length
									if dist_to_target - dist_to_static > 1.0:
										gun_target_pos = math_gun_world + gun_dir.scale(dist_to_target)
									else:
										gun_target_pos = gun_hit[0]
								else:
									gun_target_pos = math_gun_world + gun_dir.scale(dist_to_target)
							else:
								gun_target_pos = math_gun_world + gun_dir.scale(10000.0)
						else:
							gun_target_pos = math_gun_world + gun_dir.scale(10000.0)

						# UPDATE CROSSHAIR
						if hasattr(g_offline_aih, 'ctrl'):
							try:
								if hasattr(player, 'gunRotator'):
									player.gunRotator.dispersionAngle = _gun_state['dispersion']
								
								dist_m = (gun_target_pos - math_gun_world).length
								size_m = _gun_state['dispersion'] * dist_m * 2.0
								
								g_offline_aih.ctrl.updateGunMarker(gun_target_pos, gun_dir, size_m, 0.0, None)
							except Exception as e:
								LOG_DEBUG('OfflineBattle updateGunMarker error:', str(e), 'pos:', true_gun_pos, 'dir:', gun_dir)
							try:
								g_offline_aih.ctrl.updateGunMarker2(gun_target_pos, gun_dir, size_m, 0.0, None)
							except Exception as e:
								pass
								
							if _gun_state.get('tick_counter', 0) % 60 == 0:
								import debug_utils
								try:
									cam_m_debug = Math.Matrix(BigWorld.camera().matrix)
									debug_utils.LOG_DEBUG("DEBUG DIR", "cam_pos:", cam_m_debug.translation, "gun_pos:", true_gun_pos)
									debug_utils.LOG_DEBUG("DEBUG DIR", "cam_dir:", cam_m_debug.applyToAxis(2), "gun_dir:", gun_dir)
									debug_utils.LOG_DEBUG("DEBUG DIR", "tYaw:", tYaw, "gPitch:", gPitch)
								except: pass
							_gun_state['tick_counter'] = _gun_state.get('tick_counter', 0) + 1
								
							# Synchronize ammo UI when switching control modes
							aim = getattr(g_offline_aih, 'aim', None)
							if aim and aim != _gun_state.get('last_aim'):
								_gun_state['last_aim'] = aim
								try:
									if hasattr(aim, 'setClipParams'): aim.setClipParams(_gun_state['clip_size'], 1)
									if hasattr(aim, 'setAmmoStock'): aim.setAmmoStock(_gun_state['ammo_%d' % _gun_state.get('shot_index', 0)], _gun_state['clip'], False)
									if _gun_state['reloadTime'] > 0 and hasattr(aim, 'setReloading'): aim.setReloading(_gun_state['reloadTime'], None)
								except Exception as e: LOG_DEBUG('CRITICAL ERROR IN K KEY:', e); import traceback; LOG_DEBUG(traceback.format_exc())
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
				
				tm_local = Math.Matrix()
				tm_local.setRotateYPR((turret_yaw[0], gun_pitch[0], 0))
				turret_matrix_local.set(tm_local)

				# --- PLAYER FIRE LOGIC ---
				try:
					_player_mock = mock_vehicles.get(getattr(player, 'playerVehicleID', -1))
					if _player_mock and getattr(_player_mock, 'is_on_fire', False) and getattr(_player_mock, 'health', 0) > 0:
						cur_timer = getattr(_player_mock, '_fire_timer', 0.0)
						if cur_timer is None: cur_timer = 0.0
						_player_mock._fire_timer = float(cur_timer) + 0.02
						if _player_mock._fire_timer >= 1.0:
							_player_mock._fire_timer -= 1.0
							fire_dmg = max(1, int(_player_mock.maxHealth * 0.05))
							_player_mock.health -= fire_dmg
							
							try:
								import gui.WindowsManager
								bw = gui.WindowsManager.g_windowsManager.battleWindow
								import debug_utils
								debug_utils.LOG_DEBUG("PLAYER_FIRE_TICK! bw: ", bw)
								if bw:
									debug_utils.LOG_DEBUG("BW_DIR: ", dir(bw))
									if hasattr(bw, 'damagePanel'):
										debug_utils.LOG_DEBUG("DAMAGE_PANEL_DIR: ", dir(bw.damagePanel))
										bw.damagePanel.updateHealth(_player_mock.health)
							except: pass
							
							if _player_mock.health <= 0:
								_player_mock.health = 0
								player.arena.onVehicleKilled(getattr(_player_mock, 'id', player.playerVehicleID), getattr(_player_mock, 'last_killer_id', -1), 2)
								_player_mock.is_on_fire = False
								try:
									import gui.WindowsManager
									bw = gui.WindowsManager.g_windowsManager.battleWindow
									if hasattr(bw, 'damagePanel'):
										bw.damagePanel._DamagePanel__callFlash('onFireInVehicle', [False])
								except: pass
							
							if hasattr(player, 'vehicle') and player.vehicle:
								player.vehicle.health = _player_mock.health
								try: player.guiSessionProvider.invalidateVehicleState(1, player.playerVehicleID, _player_mock.health, _player_mock.health)
								except: pass
				except: pass

				# --- BOT AI (Advanced Physics) ---
				import math, random
				dt = 0.02 # approx tick delta
				for eid, m_veh in mock_vehicles.iteritems():
					if eid != getattr(player, 'playerVehicleID', -1) and getattr(m_veh, 'isAlive', False):
						try:
							my_team = getattr(m_veh, '_bot_team', m_veh.publicInfo.get('team', 2) if getattr(m_veh, 'publicInfo', None) is not None else 2)
							closest_dist = 99999.0
							target_pos = None
							# INIT BOT STATES
							if getattr(m_veh, '_veh_velocity', None) is None: m_veh._veh_velocity = 0.0
							if getattr(m_veh, '_veh_turn_velocity', None) is None: m_veh._veh_turn_velocity = 0.0
							
							# Check player
							player_team = getattr(player, '_offhangar_team', 1)
							if my_team != player_team and getattr(player, 'playerVehicleID', -1) != -1 and getattr(player, 'health', 1) > 0:
								dx = veh_pos[0] - m_veh.position.x
								dz = veh_pos[2] - m_veh.position.z
								dist = math.sqrt(dx*dx + dz*dz)
								if dist < closest_dist:
									closest_dist = dist
									target_pos = veh_pos
							# Check other bots
							for oeid, omeh in mock_vehicles.iteritems():
								if oeid == getattr(player, 'playerVehicleID', -1): continue
								if oeid != eid and getattr(omeh, 'isAlive', False):
									oteam = getattr(omeh, '_bot_team', omeh.publicInfo.get('team', 2) if getattr(omeh, 'publicInfo', None) is not None else 2)
									if my_team != oteam:
										dx = omeh.position.x - m_veh.position.x
										dz = omeh.position.z - m_veh.position.z
										dist = math.sqrt(dx*dx + dz*dz)
										if dist < closest_dist:
											closest_dist = dist
											target_pos = (omeh.position.x, omeh.position.y, omeh.position.z)
							if target_pos is None:
								# NO ENEMIES! STOP!
								m_veh._veh_velocity = max(0.0, m_veh._veh_velocity - 20.0 * dt)
								m_veh._veh_turn_velocity = 0.0
								target_pos = (m_veh.position.x, m_veh.position.y, m_veh.position.z)
							dx = target_pos[0] - m_veh.position.x
							dz = target_pos[2] - m_veh.position.z
							dist = math.sqrt(dx*dx + dz*dz)
							_dc = (getattr(m_veh, '_dbg_ctr', 0) or 0)
							if _dc % 200 == 0:
								LOG_DEBUG('BOT_AI eid=%s vel=%.2f dist=%.1f tgt=%s escape=%s' % (str(eid), m_veh._veh_velocity, dist, str(target_pos is not None), str(getattr(m_veh, '_wall_escape', 0))))

							_td = getattr(m_veh, 'typeDescriptor', None) or loaded_models.get('td')
							
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
										# chassis['rotationSpeed'] je v rad/s (ne stupně)
										raw_rot = float(_td.chassis['rotationSpeed'])
										# Pokud je hodnota > 2*pi, je ve stupních – konvertovat
										bot_chassisRotSpd = math.radians(raw_rot) if raw_rot > 6.3 else raw_rot
									elif 'rotationSpeedLimit' in _td.physics:
										# rotationSpeedLimit je již v rad/s
										bot_chassisRotSpd = float(_td.physics['rotationSpeedLimit'])
							except: pass
							
							# VIRTUAL DRIVER
							throttle = 0.0
							turn_dir = 0

							# Preliminary yaw to target (needed by feelers before blending)
							_raw_target_yaw = math.atan2(dx, dz)
							_raw_diff_yaw = _raw_target_yaw - m_veh.yaw
							while _raw_diff_yaw > math.pi:  _raw_diff_yaw -= 2*math.pi
							while _raw_diff_yaw < -math.pi: _raw_diff_yaw += 2*math.pi

							# --- STUCK DETECTOR ---
							# Track last position; if not moved >0.5m in 100 ticks (2 sec), force reverse escape
							_last_p = getattr(m_veh, '_last_pos', None)
							_cur_escape = getattr(m_veh, '_wall_escape', None) or 0
							_stuck_ctr = getattr(m_veh, '_stuck_ctr', 0) or 0
							
							if _cur_escape > 0:
								_stuck_ctr = 0
								m_veh._last_pos = (m_veh.position.x, m_veh.position.z)
							else:
								_stuck_ctr += 1
								if _stuck_ctr >= 100:
									if _last_p is not None:
										_moved = math.sqrt((m_veh.position.x-_last_p[0])**2 + (m_veh.position.z-_last_p[1])**2)
										if _moved < 0.5:
											m_veh._wall_escape = 60
											m_veh._wall_turn = 1 if _raw_diff_yaw > 0 else -1
									m_veh._last_pos = (m_veh.position.x, m_veh.position.z)
									_stuck_ctr = 0
							m_veh._stuck_ctr = _stuck_ctr
							
							_escape = getattr(m_veh, '_wall_escape', None) or 0
							if _escape > 0:
								m_veh._wall_escape = _escape - 1
								# Reversing escape: drive backwards and turn
								throttle = -0.7
								turn_dir = getattr(m_veh, '_wall_turn', 1)
								diff_yaw = _raw_diff_yaw
								target_yaw = _raw_target_yaw
							else:
								# --- SEPARATION: repulsion from nearby bots ---
								sep_x = 0.0
								sep_z = 0.0
								for _seid, _smeh in mock_vehicles.iteritems():
									if _seid == eid: continue
									_sdx = m_veh.position.x - _smeh.position.x
									_sdz = m_veh.position.z - _smeh.position.z
									_sd = math.sqrt(_sdx*_sdx + _sdz*_sdz)
									if 0.5 < _sd < 12.0:
										_w = (12.0 - _sd) / 12.0
										sep_x += (_sdx / _sd) * _w
										sep_z += (_sdz / _sd) * _w

								# --- ADVANCED MULTI-RAY SENSORS (Local Avoidance) ---
								_feeler_steer_yaw = None
								if True:
									_ray_angles = [0.0]
									_step = 0.25
									for i in range(1, 6): # up to 1.25 rad (~71 degrees)
										if _raw_diff_yaw > 0:
											_ray_angles.extend([i * _step, -i * _step])
										else:
											_ray_angles.extend([-i * _step, i * _step])
									
									# Two passes: first try a wide safe margin (2.2m), if boxed in, try a tighter margin (1.6m)
									for _hw in (2.2, 1.6):
										_center_blocked = False
										_best_clear_angle = None
										
										for _fyo in _ray_angles:
											_fy = m_veh.yaw + _fyo
											_hit = False
											
											# Width-aware sensors: Left track, Center, Right track
											_cos_fy = math.cos(_fy)
											_sin_fy = math.sin(_fy)
											
											# Dual-height rays: catch low rocks (0.7m) and tall buildings (1.5m)
											_ray_profiles = [(0.7, 7.0), (1.5, 12.0)]
											
											for _h, _dist in _ray_profiles:
												if _hit: break
												
												# 1. Terrain elevation check (Cliffs and High Hills)
												_dest_x = m_veh.position.x + _sin_fy * _dist
												_dest_z = m_veh.position.z + _cos_fy * _dist
												_dest_y = m_veh.position.y
												
												try:
													_g_hit = BigWorld.wg_collideSegment(player.spaceID, 
														Math.Vector3(_dest_x, m_veh.position.y + 4.0, _dest_z), 
														Math.Vector3(_dest_x, m_veh.position.y - 15.0, _dest_z), 128)
													if _g_hit:
														_dest_y = _g_hit[0].y
													else:
														_hit = True # Abyss / out of bounds
												except: pass
												
												if _hit: break
												
												_y_diff = _dest_y - m_veh.position.y
												if _y_diff > _dist * 0.45 or _y_diff < -_dist * 0.7:
													_hit = True
													break
													
												# 2. Obstacle check (parallel to slope)
												for _ox in (-_hw, 0.0, _hw):
													_sx = m_veh.position.x + _cos_fy * _ox
													_sz = m_veh.position.z - _sin_fy * _ox
													try:
														_fs = Math.Vector3(_sx, m_veh.position.y + _h, _sz)
														_fe = Math.Vector3(_sx + _sin_fy*_dist, _dest_y + _h, _sz + _cos_fy*_dist)
														if BigWorld.wg_collideSegment(player.spaceID, _fs, _fe, 128):
															_hit = True
															break
													except: pass
											
											if _fyo == 0.0:
												if _hit: 
													_center_blocked = True
												else:
													break # Center is clear
											else:
												if not _hit:
													_best_clear_angle = _fyo
													break
													
										if not _center_blocked:
											break # Center is clear on this margin
										if _best_clear_angle is not None:
											_feeler_steer_yaw = m_veh.yaw + _best_clear_angle
											break # Found a clear path
											
									# If even tight margin fails, we just keep current steering and let stuck detector handle reversing if we crash
								
								# Hysteresis: keep steering into the clear path for a moment
								if _feeler_steer_yaw is not None:
									m_veh._feeler_timer = 15
									m_veh._feeler_mem = _feeler_steer_yaw
								else:
									_ft = getattr(m_veh, '_feeler_timer', 0) or 0
									if _ft > 0:
										m_veh._feeler_timer = _ft - 1
										_feeler_steer_yaw = getattr(m_veh, '_feeler_mem', None)
								
								if _feeler_steer_yaw is not None:
									target_yaw = _feeler_steer_yaw
								else:
									# Blend target dir + separation
									_ndx = dx / dist if dist > 0.1 else 0.0
									_ndz = dz / dist if dist > 0.1 else 0.0
									_rdx = _ndx + sep_x * 1.5
									_rdz = _ndz + sep_z * 1.5
									target_yaw = math.atan2(_rdx, _rdz)
								diff_yaw = target_yaw - m_veh.yaw
								while diff_yaw > math.pi:  diff_yaw -= 2*math.pi
								while diff_yaw < -math.pi: diff_yaw += 2*math.pi

								if dist > 15.0:
									if abs(diff_yaw) < 0.5: throttle = 1.0
									elif abs(diff_yaw) > 2.0: throttle = -0.5
									else: throttle = 0.5

								if diff_yaw > 0.05: turn_dir = 1
								elif diff_yaw < -0.05: turn_dir = -1

							m_veh._dbg_ctr = (getattr(m_veh, '_dbg_ctr', 0) or 0) + 1
							
							# IMMOBILIZATION CHECK
							_dev_hp = getattr(m_veh, 'devices_hp', None)
							if getattr(m_veh, 'is_tracked', False) or (_dev_hp is not None and _dev_hp.get('engineHealth', 1) <= 0):
								throttle = 0.0
								turn_dir = 0.0
								
							# FIRE LOGIC (Damage Over Time)
							if getattr(m_veh, 'is_on_fire', False) and m_veh.health > 0:
								cur_timer = getattr(m_veh, '_fire_timer', 0.0)
								if cur_timer is None: cur_timer = 0.0
								m_veh._fire_timer = float(cur_timer) + float(dt if dt is not None else 0.02)
								if m_veh._fire_timer >= 1.0: # Tick every 1 second
									m_veh._fire_timer -= 1.0
									fire_dmg = max(1, int(m_veh.maxHealth * 0.05)) # 5% max HP per sec
									m_veh.health -= fire_dmg
									
									try:
										import BigWorld
										from gui import WindowsManager
										bw = getattr(WindowsManager.g_windowsManager, 'battleWindow', None)
										
										if m_veh.health <= 0:
											m_veh.health = 0
											BigWorld.player().arena.onVehicleKilled(m_veh.id, BigWorld.player().playerVehicleID, 2)
										elif bw and hasattr(bw, 'vMarkersManager'):
											player_id = getattr(BigWorld.player(), 'playerVehicleID', -1)
											if m_veh.id == player_id:
												player = BigWorld.player()
												if hasattr(player, 'vehicle') and player.vehicle:
													player.vehicle.health = m_veh.health
													try: player.guiSessionProvider.invalidateVehicleState(1, player_id, m_veh.health, m_veh.health)
													except: pass
											else:
												marker = getattr(m_veh, 'marker', None)
												if marker is not None:
													bw.vMarkersManager.onVehicleHealthChanged(marker, m_veh.health, 1, 0)
													try: bw.vMarkersManager.showVehicleDamageInfo(marker, fire_dmg, 0, 0, 1)
													except: pass
													LOG_DEBUG('Fire HP updated via marker, HP=%d' % m_veh.health)
									except: pass

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
							
							try:
								if not getattr(m_veh, '_snd_init', False):
									_engine_d = getattr(_td, 'engine', None) if _td else None
									_chassis_d = getattr(_td, 'chassis', None) if _td else None
									if hasattr(_td, 'engine') and isinstance(_td.engine, dict): _engine_d = _td.engine
									if hasattr(_td, 'chassis') and isinstance(_td.chassis, dict): _chassis_d = _td.chassis
									if _engine_d and hasattr(m_veh, '_chassis_model') and getattr(m_veh._chassis_model, 'inWorld', False): 
										m_veh._snd_engine = m_veh._chassis_model.playSound(_engine_d['sound'])
									if _chassis_d and hasattr(m_veh, '_chassis_model') and getattr(m_veh._chassis_model, 'inWorld', False): 
										m_veh._snd_tracks = m_veh._chassis_model.playSound(_chassis_d['sound'])
									if getattr(m_veh, '_chassis_model', None) and m_veh._chassis_model.inWorld: m_veh._snd_init = True
								
								cur_speed = abs(m_veh._veh_velocity)
								power_fraction = min(1.0, (cur_speed / bot_speedFwd) + (abs(throttle) * 0.3))
								load = 1.0 + (power_fraction * 2.0)
								
								if getattr(m_veh, '_snd_engine', None):
									p = m_veh._snd_engine.param('load')
									if p: p.value = load
								if getattr(m_veh, '_snd_tracks', None):
									p = m_veh._snd_tracks.param('speed')
									if p: p.value = cur_speed / bot_speedFwd
							except Exception as _e: pass
							
							# COLLISION - always checked when moving
							if m_veh._veh_velocity != 0.0:
								_hit_wall = False
								if abs(m_veh._veh_velocity) > 0.5:
									try:
										_hit_wall = _check_horizontal_collision(player.spaceID, m_veh.position, m_veh.yaw, m_veh._veh_velocity, _td)
									except: pass
								if _hit_wall:
									m_veh._veh_velocity = 0.0
								else:
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
							
							_b_ypr = _get_terrain_ypr(player.spaceID, m_veh.position, m_veh.yaw)
							m_veh.pitch = _b_ypr[1]
							m_veh.roll = _b_ypr[2]
							
							m_veh.matrix.setRotateYPR(_b_ypr)
							m_veh.matrix.translation = m_veh.position
							
							try:
								if getattr(m_veh, 'bw_entity', None) is not None and getattr(m_veh.bw_entity, 'filter', None) is not None:
									m_veh.bw_entity.filter.set(BigWorld.time(), player.spaceID, m_veh.bw_entity.id, m_veh.position, (m_veh.matrix.roll, m_veh.matrix.pitch, m_veh.matrix.yaw), 0)
							except: pass
							
							if hasattr(m_veh, '_chassis_model'):
								if not getattr(m_veh, '_servo_added', False):
									try:
										m_veh._chassis_model.addMotor(BigWorld.Servo(m_veh.matrix))
										m_veh._servo_added = True
									except: pass
									
							# Otaceni veze nezavisle
							if hasattr(m_veh, '_t_mat'):
								# Věž by měla vždy mířit na hráče (cíl), nezávisle na tom, kam se vyhýbá trup
								t_yaw = _raw_target_yaw - m_veh.yaw
								while t_yaw > math.pi: t_yaw -= 2*math.pi
								while t_yaw < -math.pi: t_yaw += 2*math.pi
								
								# Načíst limity otáčení věže/děla z dat vozidla (pro TD a arty)
								bot_gun_min_yaw = -math.pi
								bot_gun_max_yaw =  math.pi
								try:
									if _td:
										yl = None
										if hasattr(_td, 'gun') and isinstance(_td.gun, dict):
											yl = _td.gun.get('turretYawLimits', None)
										if yl is None and hasattr(_td, 'turret') and isinstance(_td.turret, dict):
											yl = _td.turret.get('yawLimits', None)
										if yl is not None:
											bot_gun_min_yaw = float(yl[0])
											bot_gun_max_yaw = float(yl[1])
											# Konverze stupňů -> radiány (hodnoty > 10 jsou ve stupních)
											if abs(bot_gun_min_yaw) > 10.0 or abs(bot_gun_max_yaw) > 10.0:
												bot_gun_min_yaw = math.radians(bot_gun_min_yaw)
												bot_gun_max_yaw = math.radians(bot_gun_max_yaw)
								except: pass
								
								has_limited_traverse = not (bot_gun_min_yaw <= -math.pi + 0.1 and bot_gun_max_yaw >= math.pi - 0.1)
								
								# TD nesmí přebíjet řízení trupu kvůli míření, pokud se právě vyhýbá překážce!
								is_avoiding_obstacle = getattr(m_veh, '_feeler_timer', 0) > 0 or (_feeler_steer_yaw is not None if '_feeler_steer_yaw' in locals() else False)
								
								if has_limited_traverse and not is_avoiding_obstacle:
									# TD/Arty: pokud je cíl mimo limity, bot musí otočit celý trup
									if t_yaw < bot_gun_min_yaw - 0.05:
										# Cíl vlevo od limitu – otočit trup doleva
										m_veh._veh_turn_velocity = -bot_chassisRotSpd
									elif t_yaw > bot_gun_max_yaw + 0.05:
										# Cíl vpravo od limitu – otočit trup doprava
										m_veh._veh_turn_velocity = bot_chassisRotSpd
									
								# Omezit věž na limity vždy
								if has_limited_traverse:
									t_yaw = max(bot_gun_min_yaw, min(bot_gun_max_yaw, t_yaw))
								
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
							if getattr(m_veh, '_ai_shoot_timer', None) is None:
								m_veh._ai_shoot_timer = 0
								m_veh._ai_clip_size = 1
								m_veh._ai_clip = 1
								m_veh._ai_reload_intra = 0.0
								m_veh._ai_reload_full = 3.0
								try:
									_g = getattr(_td, 'gun', {}) if _td else {}
									if isinstance(_g, dict):
										if 'reloadTime' in _g: m_veh._ai_reload_full = float(_g['reloadTime'])
										if 'clip' in _g and len(_g['clip']) == 2:
											m_veh._ai_clip_size = int(_g['clip'][0])
											m_veh._ai_reload_intra = float(_g['clip'][1])
											m_veh._ai_clip = m_veh._ai_clip_size
								except: pass
								
							m_veh._ai_shoot_timer += dt
							
							# Zjistit absolutní úhel, kam míří dělo
							abs_gun_yaw = m_veh.yaw + getattr(m_veh, '_turret_yaw', 0.0)
							gun_diff = target_yaw - abs_gun_yaw
							while gun_diff > math.pi: gun_diff -= 2*math.pi
							while gun_diff < -math.pi: gun_diff += 2*math.pi
							
							bot_reload = m_veh._ai_reload_intra if (m_veh._ai_clip_size > 1 and m_veh._ai_clip > 0 and m_veh._ai_clip < m_veh._ai_clip_size) else m_veh._ai_reload_full
							
							# Vystřelí jen když míří na hráče (tolerance +- 0.15 rad = ~8.5 stupně)
							if m_veh._ai_shoot_timer > bot_reload and dist < 150.0 and abs(gun_diff) < 0.15:
								m_veh._ai_shoot_timer = 0
								if m_veh._ai_clip_size > 1:
									m_veh._ai_clip -= 1
									if m_veh._ai_clip <= 0:
										m_veh._ai_clip = m_veh._ai_clip_size
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
											
											target_y = target_pos[1] if target_pos else veh_pos[1]
											dir_v = Math.Vector3(dx, (target_y+1.0) - (m_veh.position.y+1.5), dz)
											dir_v.normalise()
											# Apply Bot Dispersion (approx 0.03 rad circle)
											sigma = 0.03 / 3.0
											dir_v.x += random.gauss(0, sigma)
											dir_v.y += random.gauss(0, sigma)
											dir_v.z += random.gauss(0, sigma)
											dir_v.normalise()
											
											_vel = dir_v.scale(_speed)
											
											start_p = Math.Vector3(m_veh.position.x, m_veh.position.y + 1.5, m_veh.position.z)
											_cam_pos = BigWorld.camera().position if BigWorld.camera() else start_p
											g_projectile_mover.add(random.randint(10000, 99999), _effectsDescr, _gravity, start_p, _vel, start_p, True, _cam_pos)

											try:
												_sound_event = '/tanks/guns/gun_small/gun_small_20-45mm'
												_b_td = getattr(m_veh, 'typeDescriptor', None)
												if _b_td:
													if hasattr(_b_td.gun, 'effects') and 'shotSound' in _b_td.gun.effects:
														_sound_event = _b_td.gun.effects['shotSound']
													elif isinstance(_b_td.gun, dict) and 'effects' in _b_td.gun and 'shotSound' in _b_td.gun['effects']:
														_sound_event = _b_td.gun['effects']['shotSound']
													elif isinstance(_b_td.gun, dict) and 'shots' in _b_td.gun and len(_b_td.gun['shots']) > 0 and 'shotSound' in _b_td.gun['shots'][0]:
														_sound_event = _b_td.gun['shots'][0]['shotSound']
												if hasattr(m_veh, '_chassis_model'): m_veh._chassis_model.playSound(_sound_event)
											except: pass
											
											player_mock = mock_vehicles.get(getattr(player, 'playerVehicleID', -1))
											if player_mock:
												try: player_mock.position = veh_pos
												except: pass
											
											end_p = start_p + dir_v.scale(500.0)
											
											# Kontrola kolize se světem (terén, budovy)
											world_hit_dist = 9999.0
											try:
												world_hit = BigWorld.wg_collideSegment(player.spaceID, start_p, end_p, 128)
												if world_hit is not None:
													hit_pt = world_hit[0]
													world_hit_dist = (hit_pt - start_p).length
											except: pass
											
											# Kontrola kolize se všemi vozidly (včetně vraků)
											veh_hit_dist = 9999.0
											hit_veh = None
											hit_col = None
											
											for oeid, omeh in mock_vehicles.iteritems():
												if oeid != eid: # Nezasáhnout sám sebe
													try: omeh.position = omeh.model.position
													except: pass
													col = omeh.collideSegment(start_p, end_p)
													if col is not None and col[0] < veh_hit_dist:
														veh_hit_dist = col[0]
														hit_veh = omeh
														hit_col = col
											
											# Pokud trefil nějaké vozidlo a bylo blíž než překážka
											if hit_veh and veh_hit_dist < world_hit_dist:
												# Trefil hráče?
												my_team = m_veh.publicInfo.get('team', 2) if getattr(m_veh, 'publicInfo', None) is not None else 2
												player_team = getattr(player, '_offhangar_team', 1)
												if hit_veh == player_mock and getattr(player_mock, 'health', 0) > 0 and my_team != player_team:
													_dist, _hitAngleCos, _armor = hit_col[:3]
													pierce = _shot.get('piercingPower', (100.0, 100.0))[0]
													pierce_rng = pierce * random.uniform(0.75, 1.25)
													angle_cos = max(0.087, abs(_hitAngleCos))
													eff_armor = _armor / angle_cos
													
													LOG_DEBUG('BOT HIT PLAYER! base=%.1f eff=%.1f pierce=%.1f' % (_armor, eff_armor, pierce_rng))
													
													auto_bounce = False
													if angle_cos < 0.342 and 'HE' not in _shot['shell']['name']:
														if _shot['shell'].get('caliber', 100) <= _armor * 3:
															auto_bounce = True
															
													dmg = 0
													# DIRECTION AND FLASH FOR ALL HITS
													try:
														px = player_mock.position
														import math
														import BigWorld
														
														# Left/Right is now CORRECT, but Front/Back is inverted.
														# Keep X inverted, and INVERT Z as well.
														dx = -(m_veh.position[0] - px[0])
														dz = -(m_veh.position[2] - px[2])
														hitDirYaw = math.atan2(dx, dz)
														
														if hasattr(player, 'inputHandler') and player.inputHandler:
															_aim = getattr(player.inputHandler, 'aim', None)
															if _aim and hasattr(_aim, 'showHit'):
																isDamage = not auto_bounce and (pierce_rng >= eff_armor or 'HE' in _shot['shell']['name'])
																_aim.showHit(hitDirYaw, isDamage)
														
														if isDamage:
															fba = Math.Vector4Animation()
															fba.keyframes = [(0.0, Math.Vector4(1.0, 0.0, 0.0, 0.7)), (0.3, Math.Vector4(1.0, 0.0, 0.0, 0.7)), (1.5, Math.Vector4(1.0, 0.0, 0.0, 0.0))]
															fba.duration = 1.5
															BigWorld.flashBangAnimation(fba)
															def remove_fba(f=fba):
																try: BigWorld.removeFlashBangAnimation(f)
																except: pass
															BigWorld.callback(1.4, remove_fba)
													except Exception as e:
														LOG_DEBUG('HitDir calc err:', e)
														
													if auto_bounce or (pierce_rng < eff_armor and 'HE' not in _shot['shell']['name']):
														LOG_DEBUG('BOT RICOCHET!')
														try:
															_fm = BigWorld.player().newFakeModel()
															BigWorld.addModel(_fm)
															_fm.position = BigWorld.camera().position
															snd = _fm.getSound('/hits/hits/tank_hit_armor_ricochet')
															if snd: snd.play()
															def _rem_fm(f=_fm):
																try: BigWorld.delModel(f)
																except: pass
															BigWorld.callback(3.0, _rem_fm)
														except Exception as ex:
															LOG_DEBUG('Ricochet FM err:', ex)
														try:
															if hasattr(player.inputHandler, 'ctrl') and player.inputHandler.ctrl:
																cam = getattr(player.inputHandler.ctrl, 'camera', None)
																_dir = Math.Vector3(dx, 0, dz)
																_dir.normalise()
																if cam and hasattr(cam, 'applyImpulse'):
																	cam.applyImpulse(_dir, 0.5)
																elif cam and hasattr(cam, 'impulseOscillator') and cam.impulseOscillator:
																	cam.impulseOscillator.applyImpulse(_dir * 0.5)
														except: pass
													else:
														_dmg_base = _shot['shell']['damage'][0]
														dmg = _dmg_base * random.uniform(0.75, 1.25)
														try:
															dmg = _apply_module_damage(player_mock, hit_col[3], m_veh.position, player_mock.position, dmg, _shot['shell'], m_veh.id)
														except Exception as ex:
															import traceback
															LOG_DEBUG("PLAYER MODULE DAMAGE ERROR:", traceback.format_exc() if 'traceback' in globals() else str(ex))
														player_mock.health -= int(dmg)
														try:
															_fm = BigWorld.player().newFakeModel()
															BigWorld.addModel(_fm)
															_fm.position = BigWorld.camera().position
															snd = _fm.getSound('/hits/hits/tank_hit_armor_crit')
															if snd: snd.play()
															def _rem_fm(f=_fm):
																try: BigWorld.delModel(f)
																except: pass
															BigWorld.callback(3.0, _rem_fm)
														except Exception as ex:
															LOG_DEBUG('Pierce FM err:', ex)
														try:
															if hasattr(player.inputHandler, 'ctrl') and player.inputHandler.ctrl:
																cam = getattr(player.inputHandler.ctrl, 'camera', None)
																_dir = Math.Vector3(dx, 0, dz)
																_dir.normalise()
																if cam and hasattr(cam, 'applyImpulse'):
																	cam.applyImpulse(_dir, 1.0)
																elif cam and hasattr(cam, 'impulseOscillator') and cam.impulseOscillator:
																	cam.impulseOscillator.applyImpulse(_dir * 1.0)
														except: pass
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
																pass # bw.vMarkersManager.updateVehicleHealth(player.playerVehicleID, player_mock.health, 1, 0)
														except: pass
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
																pass # bw.vMarkersManager.updateVehicleHealth(player.playerVehicleID, player_mock.health, 1, 0)
														except: pass
												else:
													my_team = m_veh.publicInfo.get('team', 2) if getattr(m_veh, 'publicInfo', None) is not None else 2
													target_team = hit_veh.publicInfo.get('team', 2) if getattr(hit_veh, 'publicInfo', None) is not None else (getattr(player, '_offhangar_team', 1) if getattr(player, 'playerVehicleID', -1) == hit_veh.id else 2)
													if getattr(hit_veh, 'health', 0) > 0 and my_team != target_team:
														# ARMOR PENETRATION LOGIC FOR BOT vs BOT
														_dmg_base = _shot['shell']['damage'][0]
														pierce_rng = _shot.get('piercingPower', (100.0, 100.0))[0] * random.uniform(0.75, 1.25)
														
														_dist, _hitAngleCos, _armor = hit_col[:3]
														angle_cos = max(0.087, abs(_hitAngleCos))
														eff_armor = _armor / angle_cos
														
														auto_bounce = False
														if angle_cos < 0.342 and 'HE' not in _shot['shell']['name']:
															if _shot['shell'].get('caliber', 100) <= _armor * 3:
																auto_bounce = True
														
														is_damage = not auto_bounce and (pierce_rng >= eff_armor or 'HE' in _shot['shell']['name'])
														
														if is_damage:
															LOG_DEBUG('BOT HIT ENEMY BOT: PENETRATION!')
															_dmg = int(_dmg_base * random.uniform(0.75, 1.25))
															try:
																_dmg = int(_apply_module_damage(hit_veh, hit_col[3], m_veh.position, hit_veh.position, _dmg, _shot['shell'], m_veh.id))
															except Exception as ex:
																import traceback
																LOG_DEBUG("BOT MODULE DAMAGE ERROR:", traceback.format_exc() if 'traceback' in globals() else str(ex))
															hit_veh.health -= _dmg
															hit_veh.damage_from_bots = getattr(hit_veh, 'damage_from_bots', 0) + _dmg
															hit_veh.last_killer_id = m_veh.id
															try:
																player.arena.onVehicleStatisticsUpdate(hit_veh.id)
																from gui import WindowsManager
																bw = getattr(WindowsManager.g_windowsManager, 'battleWindow', None)
																if bw and hasattr(bw, 'vMarkersManager'):
																	marker = getattr(hit_veh, 'marker', None)
																	if marker is not None:
																		bw.vMarkersManager.onVehicleHealthChanged(marker, hit_veh.health, m_veh.id, 0)
																		try: bw.vMarkersManager.showVehicleDamageInfo(marker, _dmg, 0, 0, 0)
																		except: pass
																	try: bw.minimap.notifyVehicleStop(hit_veh.id) if hit_veh.health <= 0 else None
																	except: pass
															except: pass
														else:
															LOG_DEBUG('BOT HIT ENEMY BOT: RICOCHET/NON-PEN!')
														if hit_veh.health <= 0:
															hit_veh.isAlive = False
															try:
																from gui import WindowsManager
																bw = getattr(WindowsManager.g_windowsManager, 'battleWindow', None)
																if bw and hasattr(bw, '_Battle__arena'):
																	bw._Battle__arena.vehicles[hit_veh.id]['isAlive'] = False
																	bw._Battle__updatePlayers()
															except: pass
															LOG_DEBUG('BOT KILLED ENEMY BOT!')
															try:
																try: hit_veh.appearance.changeVisibility('', False, False)
																except: pass
																try:
																	_dtd = hit_veh.typeDescriptor
																	_d_ch = BigWorld.Model(_dtd.chassis['models']['destroyed'])
																	_d_hu = BigWorld.Model(_dtd.hull['models']['destroyed'])
																	_d_tu = BigWorld.Model(_dtd.turret['models']['destroyed'])
																	_d_gu = BigWorld.Model(_dtd.gun['models']['destroyed'])
																	_old_ch = hit_veh._chassis_model
																	_old_pos = _old_ch.position
																	_old_yaw = _old_ch.yaw
																	_old_ch_ref = _old_ch
																	def _swap_destroyed_model_bot(_d_ch=_d_ch, _d_hu=_d_hu, _d_tu=_d_tu, _d_gu=_d_gu, _old_ch_ref=_old_ch_ref, _old_pos=_old_pos, _old_yaw=_old_yaw, m_veh=hit_veh):
																		if not getattr(_d_ch, 'loaded', True) or not getattr(_d_hu, 'loaded', True) or not getattr(_d_tu, 'loaded', True) or not getattr(_d_gu, 'loaded', True):
																			BigWorld.callback(0.1, _swap_destroyed_model_bot)
																			return
																		try: _old_ch_ref.visible = False
																		except: pass
																		try: BigWorld.delModel(_old_ch_ref)
																		except: pass
																		_d_ch.position = _old_pos
																		_d_ch.yaw = _old_yaw
																		_h_mat = Math.Matrix(); _h_mat.setIdentity()
																		_t_mat = Math.Matrix(); _t_mat.setIdentity()
																		_g_mat = Math.Matrix(); _g_mat.setIdentity()
																		try: _d_ch.node('V').attach(_d_hu)
																		except: pass
																		try: 
																			m_veh._d_t_node = _d_hu.node('HP_turretJoint', _t_mat)
																			m_veh._d_t_node.attach(_d_tu)
																		except: pass
																		try: 
																			m_veh._d_g_node = _d_tu.node('HP_gunJoint', _g_mat)
																			m_veh._d_g_node.attach(_d_gu)
																		except: pass
																		try: BigWorld.addModel(_d_ch)
																		except: pass
																	BigWorld.callback(0.1, _swap_destroyed_model_bot)
																except Exception as e:
																	LOG_DEBUG('Swap bot destroyed model error:', e)
																
																if hasattr(player.arena, 'statistics'):
																	if eid not in player.arena.statistics: player.arena.statistics[eid] = {'frags': 0}
																	_atk_team = getattr(m_veh, '_bot_team', m_veh.publicInfo.get('team', 2) if getattr(m_veh, 'publicInfo', None) is not None else 2)
																	_vic_team = getattr(hit_veh, '_bot_team', hit_veh.publicInfo.get('team', 2) if getattr(hit_veh, 'publicInfo', None) is not None else 2)
																	_frag_diff_bot = -1 if _atk_team == _vic_team else 1
																	player.arena.vehicles[eid]['frags'] = player.arena.vehicles[eid].get('frags', 0) + _frag_diff_bot
																	player.arena.statistics[eid]['frags'] = player.arena.statistics[eid].get('frags', 0) + _frag_diff_bot
																player.arena.onVehicleKilled(hit_veh.id, eid, 0)
																try:
																	if hasattr(player, 'onVehicleKilled'): player.onVehicleKilled(hit_veh.id, eid, 0)
																except: pass
																for v_id in player.arena.vehicles:
																	if v_id not in player.arena.statistics: player.arena.statistics[v_id] = {'frags': 0}
																player.arena.onVehicleStatisticsUpdate(eid)
																if hasattr(bw, '_Battle__updatePlayers'):
																	try: bw._Battle__updatePlayers()
																	except: pass
																if hasattr(bw, '_Battle__fragCorrelation'):
																	p_team = getattr(player, '_offhangar_team', 1)
																	allied = sum(v.get('frags', 0) for i,v in player.arena.vehicles.items() if i in player.arena.statistics and v.get('team') == p_team)
																	enemy = sum(v.get('frags', 0) for i,v in player.arena.vehicles.items() if i in player.arena.statistics and v.get('team') != p_team)
																	try: bw._Battle__fragCorrelation.updateFrags(allied, enemy)
																	except: pass
																if hasattr(bw, '_Battle__pMsgsPanel'):
																	try: bw._Battle__pMsgsPanel.showMessage('PlayerKilled', {'killer': (m_veh.publicInfo.get('name', 'Bot') if getattr(m_veh, 'publicInfo', None) else 'Bot'), 'victim': (hit_veh.publicInfo.get('name', 'Bot') if getattr(hit_veh, 'publicInfo', None) else 'Bot')})
																	except: pass
															except: pass
													else:
														LOG_DEBUG('BOT MISSED PLAYER - Hit another vehicle (corpse/ally) first at dist %.1f' % veh_hit_dist)
											elif world_hit_dist < 9999.0:
												LOG_DEBUG('BOT MISSED PLAYER - Hit obstacle (terrain/building) first at dist %.1f' % world_hit_dist)
								except Exception as e: LOG_DEBUG('CRITICAL ERROR IN K KEY:', e); import traceback; LOG_DEBUG(traceback.format_exc())
						except Exception as e:
							import traceback
							LOG_DEBUG('Bot AI Exception:', traceback.format_exc())
							
				# PLAYER DEATH CHECK
				try:
					player_mock = mock_vehicles.get(getattr(player, 'playerVehicleID', -1))
					if player_mock and player_mock.health <= 0 and getattr(player, '_is_dead', False) is not True:
						player._is_dead = True
						LOG_DEBUG('Player is dead. Spawning destroyed model and ending battle.')
						try:
							killer_id = getattr(player_mock, 'last_killer_id', -1)
							p_id = player.playerVehicleID
							if killer_id != -1 and killer_id in player.arena.vehicles and hasattr(player.arena, 'onVehicleKilled'):
								player.arena.vehicles[killer_id]['frags'] = player.arena.vehicles[killer_id].get('frags', 0) + 1
								if hasattr(player.arena, 'statistics'):
									if killer_id not in player.arena.statistics: player.arena.statistics[killer_id] = {'frags': 0}
									player.arena.statistics[killer_id]['frags'] = player.arena.statistics[killer_id].get('frags', 0) + 1
								player.arena.onVehicleKilled(p_id, killer_id, 0)
								player.arena.onVehicleStatisticsUpdate(killer_id)
								
								from gui import WindowsManager
								bw = getattr(WindowsManager.g_windowsManager, 'battleWindow', None)
								if bw and hasattr(bw, '_Battle__fragCorrelation'):
									p_team = getattr(player, 'team', 1)
									allied = sum(v.get('frags', 0) for v in player.arena.vehicles.values() if v.get('team') == p_team)
									enemy = sum(v.get('frags', 0) for v in player.arena.vehicles.values() if v.get('team') != p_team)
									bw._Battle__fragCorrelation.updateFrags(allied, enemy)
									try:
										if hasattr(bw, '_Battle__pMsgsPanel'):
											bw._Battle__pMsgsPanel.showMessage('PlayerKilled', {'killer': player.arena.vehicles[killer_id].get('name', 'Bot'), 'victim': player.arena.vehicles[p_id].get('name', 'Player')})
										if hasattr(bw, '_Battle__setPlayerInfo'):
											try:
												pass
											except Exception as e:
												import inspect
												LOG_DEBUG('setPlayerInfo error:', str(e), inspect.getargspec(bw._Battle__setPlayerInfo))
												try:
													import sys
													LOG_DEBUG('battle attributes again', dir(bw))
												except: pass
									except: pass
						except Exception as _e:
							LOG_DEBUG('Frag update error:', _e)
						
						# Swap model - hide live models, show destroyed ones
						try:
							_dtd = getattr(player_mock, 'typeDescriptor', None) or loaded_models.get('td')
							_d_ch = BigWorld.Model(_dtd.chassis['models']['destroyed'])
							_d_hu = BigWorld.Model(_dtd.hull['models']['destroyed'])
							_d_tu = BigWorld.Model(_dtd.turret['models']['destroyed'])
							_d_gu = BigWorld.Model(_dtd.gun['models']['destroyed'])
							
							def _swap_player_destroyed(_d_ch=_d_ch, _d_hu=_d_hu, _d_tu=_d_tu, _d_gu=_d_gu):
								try:
									# Force load
									_add_model(_d_ch)
									_add_model(_d_hu)
									_add_model(_d_tu)
									_add_model(_d_gu)
									
									def _attach_when_ready():
										if not getattr(_d_ch, 'loaded', True) or not getattr(_d_hu, 'loaded', True) or not getattr(_d_tu, 'loaded', True) or not getattr(_d_gu, 'loaded', True):
											BigWorld.callback(0.1, _attach_when_ready)
											return
										try: BigWorld.delModel(_d_hu)
										except: pass
										try: BigWorld.delModel(_d_tu)
										except: pass
										try: BigWorld.delModel(_d_gu)
										except: pass
										
										_live_chassis = loaded_models.get('chassis') or loaded_models.get('hull')
										if _live_chassis is not None:
											try:
												for _mot in list(_live_chassis.motors):
													_live_chassis.delMotor(_mot)
											except: pass
											try: _live_chassis.visible = False
											except: pass
											try: BigWorld.delModel(_live_chassis)
											except: pass
										
										try: _d_ch.node('V').attach(_d_hu)
										except: pass
										try: _d_hu.node('HP_turretJoint').attach(_d_tu)
										except: pass
										try: _d_tu.node('HP_gunJoint').attach(_d_gu)
										except: pass
										
										_d_ch.position = Math.Vector3(mock_veh.position)
										try: _d_ch.addMotor(BigWorld.Servo(chassis_mp))
										except: pass
										try:
											mock_veh._collision_obstacle = BigWorld.PyModelObstacle(
												_ptd.hull['models']['destroyed'],
												_ptd.turret['models']['destroyed'],
												chassis_mp,
												False
											)
										except: pass
										LOG_DEBUG('Player destroyed model placed OK')
									_attach_when_ready()
								except Exception as _e:
									import traceback
									LOG_DEBUG('Player model swap failed:', traceback.format_exc())
							
							BigWorld.callback(0.1, _swap_player_destroyed)
						except Exception as _e: LOG_DEBUG('Player death model err:', str(_e))
						
						# Exit battle in 5 seconds - use game.fini() which is the proper hook
						def _exit_battle():
							try:
								LOG_DEBUG('Player death: triggering exit to hangar')
								_battle_finished[0] = True
								try:
									import SoundGroups as _SG
									if getattr(_SG, 'g_instance', None) is not None:
										_SG.g_instance.enableArenaSounds(False)
										_SG.g_instance.enableLobbySounds(True)
								except Exception as e: LOG_DEBUG('CRITICAL ERROR IN K KEY:', e); import traceback; LOG_DEBUG(traceback.format_exc())
								
								# Safely stop all control_modes ticks before they can crash
								try:
									if not hasattr(player, 'soundNotifications'):
										try:
											from gui.Scaleform import IngameSoundNotifications
											player.soundNotifications = IngameSoundNotifications.IngameSoundNotifications()
											player.soundNotifications.start()
										except Exception as e:
											try:
												from gui import IngameSoundNotifications
												player.soundNotifications = IngameSoundNotifications.IngameSoundNotifications()
												player.soundNotifications.start()
											except Exception as e: pass
								except: pass
								
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
										except Exception as e: LOG_DEBUG('CRITICAL ERROR IN K KEY:', e); import traceback; LOG_DEBUG(traceback.format_exc())
									g_offline_models = []
								except Exception as e: LOG_DEBUG('CRITICAL ERROR IN K KEY:', e); import traceback; LOG_DEBUG(traceback.format_exc())
								
								try:
									global g_projectile_mover
									if g_projectile_mover is not None:
										g_projectile_mover.destroy()
								except Exception as e: LOG_DEBUG('CRITICAL ERROR IN K KEY:', e); import traceback; LOG_DEBUG(traceback.format_exc())
								
								try:
									BigWorld.camera(None)
									BigWorld.worldDrawEnabled(False)
								except Exception as e: LOG_DEBUG('CRITICAL ERROR IN K KEY:', e); import traceback; LOG_DEBUG(traceback.format_exc())
								
								try:
									import gui.ClientHangarSpace
									LOG_DEBUG('ClientHangarSpace module dir:', dir(gui.ClientHangarSpace))
									LOG_DEBUG('ClientHangarSpace class dir:', dir(gui.ClientHangarSpace.ClientHangarSpace))
								except Exception as e:
									LOG_DEBUG('ClientHangarSpace error:', e)
								
								try:
									BigWorld.worldDrawEnabled(True)
								except Exception as e: LOG_DEBUG('CRITICAL ERROR IN K KEY:', e); import traceback; LOG_DEBUG(traceback.format_exc())
									
								try:
									from gui import WindowsManager
									
									if hasattr(WindowsManager.g_windowsManager, 'showLobby'):
										WindowsManager.g_windowsManager.showLobby()
										LOG_DEBUG('Triggered showLobby() for full UI and camera reload!')
										
									from gui.Scaleform.utils.HangarSpace import g_hangarSpace
									if g_hangarSpace is not None:
										try:
											g_hangarSpace.destroy()
										except Exception as e: LOG_DEBUG('CRITICAL ERROR IN K KEY:', e); import traceback; LOG_DEBUG(traceback.format_exc())
										g_hangarSpace.init(True)
										g_hangarSpace.refreshVehicle()
										LOG_DEBUG('Restored HangarSpace via global instance!')
									else:
										LOG_DEBUG('Global g_hangarSpace is None!')
										
								except Exception as e:
									import traceback
									LOG_DEBUG('HangarSpace restore error:', traceback.format_exc())
								
								try:
									BigWorld.worldDrawEnabled(True)
								except Exception as e: LOG_DEBUG('CRITICAL ERROR IN K KEY:', e); import traceback; LOG_DEBUG(traceback.format_exc())
									
								# Set the allow flag and trigger native exit
								for _e in BigWorld.entities.values():
									if _e.__class__.__name__ in ('PlayerAccount', 'Account'):
										_e._offline_allow_become_non_player = True
										if hasattr(_e, '_offhangar_orig_stats') and _e._offhangar_orig_stats is not None:
											_e.stats = _e._offhangar_orig_stats
										try: _e.showGUI(_c.OFFLINE_GUI_CTX)
										except Exception as e: LOG_DEBUG('CRITICAL ERROR IN K KEY:', e); import traceback; LOG_DEBUG(traceback.format_exc())
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
			_orig_cam_update = getattr(_cams.SniperCamera, '_orig_cam_update', None)
			if not _orig_cam_update:
				_orig_cam_update = _cams.SniperCamera._SniperCamera__cameraUpdate
				_cams.SniperCamera._orig_cam_update = _orig_cam_update
			_mv_ref = mock_veh
			_vm_ref = veh_matrix
			def _patched_cam_update(cam_self, *a, **kw):
				_orig_cam_update(cam_self, *a, **kw)
				try:
					cam = getattr(cam_self, '_SniperCamera__cam', None)
					if cam is not None and hasattr(cam, 'source'):
						if 'gun_node_matrix' in loaded_models:
							cam.source = loaded_models['gun_node_matrix']
						else:
							mp = Math.WGTranslationOnlyMP()
							mp.source = _vm_ref
							cam.source = mp
				except Exception:
					pass
			_cams.SniperCamera._SniperCamera__cameraUpdate = _patched_cam_update
			_cams.SniperCamera._offhangar_patched = True
			LOG_DEBUG('OfflineBattle.SniperCamera.__cameraUpdate patched')
		except Exception:
			LOG_CURRENT_EXCEPTION()

		# Patch control_modes and cameras ticks to stop gracefully after player is gone
		try:
			import AvatarInputHandler.control_modes as _ctrl
			import AvatarInputHandler.cameras as _cams2
			
			if hasattr(_ctrl.ArcadeControlMode, '_ArcadeControlMode__tick') and not hasattr(_ctrl.ArcadeControlMode, '_offhangar_patched'):
				# Patch ArcadeControlMode.__tick
				_orig_ctrl_tick = getattr(_ctrl.ArcadeControlMode, '_ArcadeControlMode__tick')
				def _safe_ctrl_tick(self_cm, *a, **kw):
					if BigWorld.player() is None:
						return  # Stop ticking after battle ends
					return _orig_ctrl_tick(self_cm, *a, **kw)
				_ctrl.ArcadeControlMode._ArcadeControlMode__tick = _safe_ctrl_tick
				_ctrl.ArcadeControlMode._offhangar_patched = True
				
			if hasattr(_cams2, 'ArcadeCamera') and hasattr(_cams2.ArcadeCamera, '_ArcadeCamera__cameraUpdate') and not hasattr(_cams2.ArcadeCamera, '_offhangar_patched'):
				# Patch ArcadeCamera.__cameraUpdate
				_orig_arc_cam = getattr(_cams2.ArcadeCamera, '_ArcadeCamera__cameraUpdate')
				def _safe_arc_cam(self_ac, *a, **kw):
					if BigWorld.player() is None:
						return
					return _orig_arc_cam(self_ac, *a, **kw)
				_cams2.ArcadeCamera._ArcadeCamera__cameraUpdate = _safe_arc_cam
				_cams2.ArcadeCamera._offhangar_patched = True
			
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
			import Avatar
			class _FakeAvatarMod(object):
				PlayerAvatar = type(player)
			
			if hasattr(gui.Scaleform.Battle, 'Avatar'):
				gui.Scaleform.Battle.orig_Avatar = gui.Scaleform.Battle.Avatar
			gui.Scaleform.Battle.Avatar = _FakeAvatarMod
			
			if hasattr(Avatar, 'PlayerAvatar'):
				Avatar.orig_PlayerAvatar = Avatar.PlayerAvatar
			Avatar.PlayerAvatar = type(player)
			
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
				
			import types
			if hasattr(player, 'getOwnVehicleShotDispersionAngle'):
				if getattr(player.getOwnVehicleShotDispersionAngle, '__name__', '') != '_mock_getOwnVehicleShotDispersionAngle':
					_orig_get_disp = player.getOwnVehicleShotDispersionAngle
					def _mock_getOwnVehicleShotDispersionAngle(self, turretRotationSpeed, withShot=0):
						orig = _orig_get_disp(turretRotationSpeed, withShot)
						return (_gun_state.get('dispersion', orig[0]), orig[1])
					player.getOwnVehicleShotDispersionAngle = types.MethodType(_mock_getOwnVehicleShotDispersionAngle, player)
			
			# VŽDY resetuj životní funkce při nové bitvě
			player.isVehicleAlive = True
			player._is_dead = False
			player._crosshair_init_done = False
			if hasattr(player, 'vehicle') and player.vehicle is not None:
				try: player.vehicle.typeDescriptor = td
				except Exception: pass
				player.vehicle.health = getattr(td, 'maxHealth', 400)
				player.vehicle.isAlive = True
				
			if not hasattr(player, 'name'):
				player.name = 'Player'
			if not hasattr(player, 'team'):
				player.team = 1
			
			

			


			def _apply_module_damage(target_mock, all_hits, start_pos, end_pos, dmg, _shell, attacker_id):
				import BigWorld, Math, random
				if getattr(target_mock, 'id', -1) == getattr(BigWorld.player(), 'playerVehicleID', -1):
					return dmg # Disable module damage for player
				if getattr(target_mock, 'devices_hp', None) is None:
					target_mock.devices_hp = {}
				
				_shell_dmg = dmg
				if _shell and 'deviceDamage' in _shell:
					_device_dmg = _shell['deviceDamage'][0] if type(_shell['deviceDamage']) is tuple else _shell['deviceDamage']
					_shell_dmg = random.uniform(_device_dmg * 0.75, _device_dmg * 1.25)
				
				is_player_attacker = (attacker_id == getattr(BigWorld.player(), 'playerVehicleID', -1))
				target_mock.last_sound = 'armor_pierced_by_player' if is_player_attacker else 'armor_pierced'
				
				has_internal = any(getattr(h[2], 'vehicleDamageFactor', 1.0) == 0.0 and getattr(getattr(h[2], 'extra', None), 'name', '') not in ('leftTrackHealth', 'rightTrackHealth', 'gunHealth') for h in all_hits if h[2])
				if not has_internal:
					w2v = Math.Matrix(target_mock.matrix)
					w2v.invert()
					veh_start = w2v.applyPoint(start_pos)
					veh_end = w2v.applyPoint(end_pos)
					vec = veh_end - veh_start
					vec_norm = Math.Vector3(vec)
					vec_norm.normalise()
					
					for h in list(all_hits):
						h_dist, h_angle, h_mat, h_comp = h
						if h_mat is not None and getattr(h_mat, 'vehicleDamageFactor', 1.0) > 0.0:
							v_pos = veh_start + vec_norm * (h_dist + 0.5)
							td = getattr(target_mock, 'typeDescriptor', getattr(BigWorld.player(), 'vehicleTypeDescriptor', None))
							ctype = 'hull' if h_comp is getattr(td, 'hull', None) else ('turret' if h_comp is getattr(td, 'turret', None) else '')
							f_name = None
							if ctype == 'hull':
								if v_pos.z < -0.5: f_name = 'engineHealth'
								else: f_name = 'ammoBayHealth'
							elif ctype == 'turret':
								f_name = 'ammoBayHealth'
							
							if f_name:
								f_extra = td.extrasDict.get(f_name) if td else None
								if f_extra:
									class FakeMat:
										vehicleDamageFactor = 0.0
										extra = f_extra
									all_hits.append((h_dist + 0.5, h_angle, FakeMat(), h_comp))
									break
				for h in all_hits:
					h_dist, h_angle, h_mat, h_comp = h
					if h_mat is not None and getattr(h_mat, 'vehicleDamageFactor', 1.0) == 0.0:
						_extra = getattr(h_mat, 'extra', None)
						if _extra is not None:
							_name = getattr(_extra, 'name', 'Unknown')
							if _name not in ('leftTrackHealth', 'rightTrackHealth', 'gunHealth'):
								saving_throw = 0.33
								if 'ammo' in _name.lower(): saving_throw = 0.60
								elif 'engine' in _name.lower(): saving_throw = 0.45
								elif 'fuel' in _name.lower(): saving_throw = 0.45
								elif 'track' in _name.lower(): saving_throw = 1.0

								if random.random() < saving_throw:
									max_hp = getattr(_extra, 'maxHealth', 100)
									current_hp = target_mock.devices_hp.get(_name, max_hp)
									current_hp -= _shell_dmg
									target_mock.devices_hp[_name] = current_hp
									
									target_mock.last_sound = 'armor_pierced_crit_by_player' if is_player_attacker else 'armor_pierced_crit'
									
									# Update Player HUD Damage Panel
									if not is_player_attacker and getattr(target_mock, 'id', getattr(BigWorld.player(), 'playerVehicleID', -1)) == getattr(BigWorld.player(), 'playerVehicleID', -1):
										try:
											player = BigWorld.player()
											dev_state = 'destroyed' if current_hp <= 0 else 'critical'
											ui_name = _name.replace('Health', '')
											if ui_name == 'leftTrack' or ui_name == 'rightTrack': ui_name = 'track'
											try: player.guiSessionProvider.invalidateVehicleState(2, player.playerVehicleID, ui_name, dev_state)
											except: pass
											
											import gui.WindowsManager
											bw = gui.WindowsManager.g_windowsManager.battleWindow
											if hasattr(bw, 'damagePanel'):
												import debug_utils
												debug_utils.LOG_DEBUG('DAMAGE_PANEL_DIR: ', dir(bw.damagePanel))
												bw.damagePanel.updateDeviceState(ui_name, dev_state)
										except Exception as e:
											import debug_utils
											debug_utils.LOG_DEBUG('UPDATE_DEVICE_ERR: ', e)

									if 'ammo' in _name.lower() and current_hp <= 0:
										dmg = target_mock.health + 10
										target_mock._is_killed = True
										target_mock.last_sound = 'enemy_killed_by_player' if is_player_attacker else 'enemy_killed'
										if is_player_attacker:
											try:
												if hasattr(BigWorld.player(), 'soundNotifications') and BigWorld.player().soundNotifications is not None:
													BigWorld.player().soundNotifications.play('enemy_killed_by_player')
											except: pass
										try:
											BigWorld.player().arena.onVehicleKilled(target_mock.id, attacker_id, 1)
										except: pass
										break
									
									if ('engine' in _name.lower() or 'fuel' in _name.lower()) and current_hp <= 0:
										if not getattr(target_mock, 'is_on_fire', False):
											target_mock.is_on_fire = True
											import debug_utils
											debug_utils.LOG_DEBUG("FIRE IGNITED ON: ", getattr(target_mock, 'id', 'PLAYER'))
											if not is_player_attacker and getattr(target_mock, 'id', getattr(BigWorld.player(), 'playerVehicleID', -1)) == getattr(BigWorld.player(), 'playerVehicleID', -1):
												try:
													import gui.WindowsManager
													bw = gui.WindowsManager.g_windowsManager.battleWindow
													if hasattr(bw, 'damagePanel'):
														bw.damagePanel._DamagePanel__callFlash('onFireInVehicle', [True])
												except Exception as e:
													debug_utils.LOG_DEBUG("FIRE UI UPDATE ERR: ", e)
									
									if 'track' in _name.lower() and current_hp <= 0:
										if not getattr(target_mock, 'is_tracked', False):
											target_mock.is_tracked = True
				return dmg

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
					import math
					jump = _gun_state['base_dispersion'] * _gun_state['after_shot']
					_gun_state['dispersion'] = math.sqrt(_gun_state['dispersion']**2 + jump**2)
					max_disp = _gun_state['base_dispersion'] * 15.0
					if _gun_state['dispersion'] > max_disp:
						_gun_state['dispersion'] = max_disp
					
					if _gun_state['clip'] > 0:
						_gun_state['reloadTime'] = _gun_state['clip_reload']
					else:
						_gun_state['reloadTime'] = _gun_state['reload']
						
					if hasattr(BigWorld.player(), 'gunRotator'):
						BigWorld.player().gunRotator.dispersionAngle = _gun_state['dispersion']
						
					player = BigWorld.player()
					player._offhangar_shots_fired = getattr(player, '_offhangar_shots_fired', 0) + 1
						
					# UPDATE RELOAD UI
					try:
						from gui import WindowsManager
						panel = WindowsManager.g_windowsManager.battleWindow.consumablesPanel
						if panel:
							shot_idx = _gun_state.get('shot_index', 0)
							panel.setShellQuantityInSlot(shot_idx, _gun_state['ammo_%d' % shot_idx], _gun_state['clip'])
							try: panel.setCoolDownTime(shot_idx, 0.0)
							except Exception as e: LOG_DEBUG('setCoolDownTime reset error:', str(e))
							try: panel.setCoolDownTime(shot_idx, _gun_state['reloadTime'])
							except Exception as e: LOG_DEBUG('setCoolDownTime error:', str(e))
						aim = getattr(g_offline_aih, 'aim', None)
						if aim:
							try: aim.setReloading(0.0, None)
							except: pass
							try: aim.setReloading(_gun_state['reloadTime'], None)
							except Exception as e: LOG_DEBUG('setReloading error:', str(e))
							shot_idx = _gun_state.get('shot_index', 0)
							aim.setAmmoStock(_gun_state['ammo_%d' % shot_idx], _gun_state['clip'], False)
					except Exception as e:
						LOG_DEBUG('Normal shoot UI error:', str(e))
					
					try:
						player._Avatar__shotWaitingTimerID = None
					except: pass
					
					# --- RAYCAST HIT DETECTION ---
					start_pos, dir_vec = player.gunRotator._VehicleGunRotator__getCurShotPosition()
					dir_vec.normalise()
					
					# Apply Player Dispersion based on actual aiming circle
					disp_angle = getattr(player.gunRotator, 'dispersionAngle', _gun_state.get('dispersion', 0.02))
					sigma = disp_angle / 3.0
					dir_vec.x += random.gauss(0, sigma)
					dir_vec.y += random.gauss(0, sigma)
					dir_vec.z += random.gauss(0, sigma)
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
					
					hit_dist = 99999.0
					enemy_mock = None
					enemy_hit_info = None
					end_pos = start_pos + dir_vec.scale(5000.0)
					
					for eid, m_veh in mock_vehicles.iteritems():
						if eid != player.playerVehicleID and getattr(m_veh, 'isAlive', False):
							# Sync stored position with model for future checks
							try: m_veh.position = m_veh.model.position
							except: pass
							col = m_veh.collideSegment(start_pos, end_pos)
							if col is not None and col[0] < hit_dist:
								hit_dist = col[0]
								enemy_mock = m_veh
								enemy_hit_info = col
					
					if enemy_mock and enemy_hit_info:
						# Calculate real damage from gun.shots[i].shell descriptor
						try:
							_td = loaded_models.get('td')
							_gun = _td.gun
							_shots = _gun.get('shots', [])
							_sidx = _gun_state.get('shot_index', 0)
							_sidx = min(_sidx, len(_shots) - 1) if _shots else 0
							_shell = _shots[_sidx].get('shell') if _shots else None
							
							dmg = 0
							if _shell and 'damage' in _shell:
								_dmg_data = _shell['damage']
								if hasattr(_dmg_data, '__len__') and len(_dmg_data) >= 1: avg = float(_dmg_data[0])
								else: avg = float(_dmg_data)
								dmg = int(random.uniform(avg * 0.75, avg * 1.25))
								
								# ARMOR PENETRATION LOGIC (Real HitBox)
								pierce = _shots[_sidx].get('piercingPower', (100.0, 100.0))[0]
								pierce_rng = pierce * random.uniform(0.75, 1.25)
								
								_dist, _hitAngleCos, _armor = enemy_hit_info[:3]
								all_hits = enemy_hit_info[3] if len(enemy_hit_info) > 3 else []
								
								# Minimum angle cos to avoid infinity (85 degrees max)
								angle_cos = max(0.087, abs(_hitAngleCos))
								eff_armor = _armor / angle_cos
								
								LOG_DEBUG('REAL ARMOR: base=%.1f eff=%.1f pierce=%.1f angle_cos=%.2f' % (_armor, eff_armor, pierce_rng, angle_cos))
								
								auto_bounce = False
								# 70 degree auto-bounce rule (cos(70) ~ 0.342), except for HE
								if angle_cos < 0.342 and 'HE' not in _shell['name']:
									# Overmatch rule: if caliber > 3 * armor, no auto-bounce
									caliber = _shell.get('caliber', 100) # Default to 100mm if unknown
									if caliber <= _armor * 3:
										auto_bounce = True
								
								if auto_bounce:
									dmg = 0
									LOG_DEBUG('REAL RICOCHET (Auto-Bounce >70 deg)!')
									import SoundGroups
									pass # removed playSound2D
								elif pierce_rng < eff_armor and 'HE' not in _shell['name']:
									dmg = 0
									LOG_DEBUG('REAL RICOCHET / NON-PENETRATION!')
									import SoundGroups
									pass # removed playSound2D
							else:
								dmg = random.randint(250, 450)
						except Exception as e:
							import traceback
							LOG_DEBUG('Damage calc error:', traceback.format_exc())
							dmg = random.randint(250, 450)
						
						if dmg > 0:
							try:
								dmg = _apply_module_damage(enemy_mock, all_hits, start_pos, end_pos, dmg, _shell, getattr(player, 'playerVehicleID', -1))
								
								try:
									snd_str = getattr(enemy_mock, 'last_sound', 'armor_pierced_by_player')
								except: pass
							except Exception as ex:
								import traceback
								LOG_DEBUG("MODULE DAMAGE ERROR:", traceback.format_exc())

							actual_dmg = min(dmg, max(0, enemy_mock.health))
							enemy_mock.health -= dmg
							enemy_mock.damage_from_player = getattr(enemy_mock, 'damage_from_player', 0) + actual_dmg
							enemy_mock.hits_from_player = getattr(enemy_mock, 'hits_from_player', 0) + 1
							LOG_DEBUG('HIT! Damage:', dmg, 'Enemy HP:', enemy_mock.health)
							
							try:
								sound_str = 'enemy_killed_by_player' if enemy_mock.health <= 0 else getattr(enemy_mock, 'last_sound', 'armor_pierced_by_player')
								if hasattr(player, 'soundNotifications') and player.soundNotifications is not None:
									player.soundNotifications.play(sound_str)
								else:
									if not hasattr(g_offline_aih, '_snd_notif'):
										try:
											from gui.IngameSoundNotifications import IngameSoundNotifications
											g_offline_aih._snd_notif = IngameSoundNotifications()
											g_offline_aih._snd_notif.start()
										except: pass
									if hasattr(g_offline_aih, '_snd_notif'):
										g_offline_aih._snd_notif.play(sound_str)
							except Exception as e:
								LOG_DEBUG('Hit sound error:', str(e))
						
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
							try:
								from gui import WindowsManager
								bw = getattr(WindowsManager.g_windowsManager, 'battleWindow', None)
								if bw and hasattr(bw, '_Battle__arena'):
									bw._Battle__arena.vehicles[enemy_mock.id]['isAlive'] = False
									bw._Battle__updatePlayers()
							except: pass
							LOG_DEBUG('ENEMY DESTROYED!')
							try:
								p_id = getattr(player, 'playerVehicleID', -1)
								if p_id != -1 and p_id in player.arena.vehicles and hasattr(player.arena, 'onVehicleKilled'):
									_pteam = getattr(player, '_offhangar_team', 1)
									_vteam = getattr(enemy_mock, '_bot_team', enemy_mock.publicInfo.get('team', 2) if getattr(enemy_mock, 'publicInfo', None) is not None else 2)
									_frag_diff = -1 if _pteam == _vteam else 1
									player.arena.vehicles[p_id]['frags'] = player.arena.vehicles[p_id].get('frags', 0) + _frag_diff
									if _frag_diff == -1:
										player.arena.vehicles[p_id]['isTeamKiller'] = True
										player.isTeamKiller = True
										LOG_DEBUG('ARENA DIR: %s' % dir(player.arena))
										try: player.arena.onTeamKiller(p_id)
										except Exception as e: LOG_DEBUG('onTeamKiller error:', str(e))
										try:
											from gui import WindowsManager
											bw = getattr(WindowsManager.g_windowsManager, 'battleWindow', None)
											if bw and hasattr(bw, '_Battle__vehicles'):
												try:
													if hasattr(bw, '_Battle__arena'):
														bw._Battle__arena.vehicles[p_id]['isTeamKiller'] = True
														LOG_DEBUG('Updated bw._Battle__arena for p_id')
													bw._Battle__updatePlayers()
													if hasattr(bw, '_Battle__onTeamKiller'):
														bw._Battle__onTeamKiller(p_id)
												except Exception as e: LOG_DEBUG('Update __arena error:', str(e))
										except Exception as e: LOG_DEBUG('BW VEHS ERROR:', str(e))
										try: player.arena.onVehicleUpdated(p_id)
										except: pass
										try: player.arena.onVehicleAdded(p_id)
										except: pass
									if hasattr(player.arena, 'statistics'):
										if p_id not in player.arena.statistics: player.arena.statistics[p_id] = {'frags': 0}
										player.arena.statistics[p_id]['frags'] = player.arena.statistics[p_id].get('frags', 0) + _frag_diff
									player.arena.onVehicleKilled(enemy_mock.id, p_id, 0)
									for v_id in player.arena.vehicles:
										if v_id not in player.arena.statistics: player.arena.statistics[v_id] = {'frags': 0}
									player.arena.onVehicleStatisticsUpdate(p_id)
									if hasattr(bw, '_Battle__updatePlayers'):
										try: bw._Battle__updatePlayers()
										except Exception as e: LOG_DEBUG('updatePlayers error:', e)
									LOG_DEBUG('FRAGS AFTER:', player.arena.vehicles[p_id].get('frags'))
									LOG_DEBUG('ARENA HAS STATS:', hasattr(player.arena, 'statistics'))
									from gui import WindowsManager
									bw = getattr(WindowsManager.g_windowsManager, 'battleWindow', None)
									if bw and hasattr(bw, '_Battle__fragCorrelation'):
										p_team = getattr(player, 'team', 1)
										allied = sum(v.get('frags', 0) for v in player.arena.vehicles.values() if v.get('team') == p_team)
										enemy = sum(v.get('frags', 0) for v in player.arena.vehicles.values() if v.get('team') != p_team)
										bw._Battle__fragCorrelation.updateFrags(allied, enemy)
										try:
											if hasattr(bw, '_Battle__pMsgsPanel'):
												bw._Battle__pMsgsPanel.showMessage('PlayerKilled', {'killer': player.arena.vehicles[p_id].get('name', 'Player'), 'victim': enemy_mock.publicInfo.get('name', 'Bot')})
											if hasattr(bw, '_Battle__setPlayerInfo'):
												try:
													pass
												except Exception as e:
													import inspect
												LOG_DEBUG('setPlayerInfo error:', str(e), inspect.getargspec(bw._Battle__setPlayerInfo))
												try:
													import sys
													LOG_DEBUG('battle attributes again', dir(bw))
												except: pass
										except: pass
							except Exception as _e:
								LOG_DEBUG('Frag update error:', _e)
							
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
									_add_model(_d_ch)
									_add_model(_d_hu)
									_add_model(_d_tu)
									_add_model(_d_gu)
									def _attach_when_ready():
										if not getattr(_d_ch, 'loaded', True) or not getattr(_d_hu, 'loaded', True) or not getattr(_d_tu, 'loaded', True) or not getattr(_d_gu, 'loaded', True):
											BigWorld.callback(0.1, _attach_when_ready)
											return
										try: BigWorld.delModel(_d_hu)
										except: pass
										try: BigWorld.delModel(_d_tu)
										except: pass
										try: BigWorld.delModel(_d_gu)
										except: pass
										try: _old_ch_ref.visible = False
										except: pass
										try: BigWorld.delModel(_old_ch_ref)
										except: pass
										
										if getattr(m_veh, 'bw_entity', None) is not None:
											try: m_veh.bw_entity.model = None
											except:
												try: m_veh.bw_entity.model = BigWorld.Model('')
												except: pass
										
										_d_ch.position = _old_pos
										_d_ch.yaw = _old_yaw
										_t_mat = Math.Matrix(); _t_mat.setIdentity()
										_g_mat = Math.Matrix(); _g_mat.setIdentity()
										try: _d_ch.node('V').attach(_d_hu)
										except: pass
										try: 
											m_veh._d_t_node = _d_hu.node('HP_turretJoint', _t_mat)
											m_veh._d_t_node.attach(_d_tu)
										except: pass
										try: 
											m_veh._d_g_node = _d_tu.node('HP_gunJoint', _g_mat)
											m_veh._d_g_node.attach(_d_gu)
										except: pass
										try:
											m_veh._collision_obstacle = BigWorld.PyModelObstacle(
												_dtd.hull['models']['destroyed'],
												_dtd.turret['models']['destroyed'],
												m_veh.matrix,
												False
											)
										except: pass
										LOG_DEBUG('Destroyed model swapped OK')
									_attach_when_ready()
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
				player = BigWorld.player()
				
				if event.key == Keys.KEY_RIGHTMOUSE:
					if event.isKeyDown():
						_gun_state['rmb_down'] = True
						bot = getattr(player, '_outlined_bot', None)
						prev_target = getattr(player, '_autoaim_target', None)
						if bot is not None:
							team = getattr(bot, '_bot_team', 2)
							player_team = getattr(player, '_offhangar_team', 1)
							if team != player_team and getattr(bot, 'health', 0) > 0:
								if prev_target == bot:
									player._autoaim_target = None
								else:
									player._autoaim_target = bot
							else:
								player._autoaim_target = None
						else:
							player._autoaim_target = None
							
						curr_target = getattr(player, '_autoaim_target', None)
						if prev_target != curr_target:
							import debug_utils
							debug_utils.LOG_DEBUG('Autoaim state changed:', prev_target, '->', curr_target)
							try:
								sound_str = 'target_captured' if curr_target is not None else 'target_unlocked'
								if hasattr(player, 'soundNotifications') and player.soundNotifications is not None:
									player.soundNotifications.play(sound_str)
								else:
									if not hasattr(g_offline_aih, '_snd_notif'):
										try:
											from gui.IngameSoundNotifications import IngameSoundNotifications
											g_offline_aih._snd_notif = IngameSoundNotifications()
											g_offline_aih._snd_notif.start()
										except: pass
									if hasattr(g_offline_aih, '_snd_notif'):
										g_offline_aih._snd_notif.play(sound_str)
										debug_utils.LOG_DEBUG('Played sound via IngameSoundNotifications')
							except Exception as e:
								debug_utils.LOG_DEBUG('Sound error:', str(e))
						
						if getattr(player, '_autoaim_target', None) is None:
							_gun_state['locked_local_yaw'] = turret_yaw[0]
							_gun_state['locked_local_pitch'] = gun_pitch[0]
					else:
						_gun_state['rmb_down'] = False
						
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
								try: panel.setCoolDownTime(idx, 0.0)
								except Exception as e:
									import debug_utils; debug_utils.LOG_DEBUG('setCoolDownTime reset error switch:', str(e))
								try: panel.setCoolDownTime(idx, _gun_state['reloadTime'])
								except Exception as e:
									import debug_utils; debug_utils.LOG_DEBUG('setCoolDownTime error switch:', str(e))
								try:
									aim = getattr(g_offline_aih, 'aim', None)
									if aim:
										try: aim.setReloading(0.0, None)
										except: pass
										aim.setReloading(_gun_state['reloadTime'], None)
										aim.setAmmoStock(_gun_state['ammo_%d' % idx], _gun_state['clip'], False)
								except Exception as e:
									import debug_utils; debug_utils.LOG_DEBUG('aim error switch:', str(e))
					except Exception as e:
						import debug_utils
						debug_utils.LOG_DEBUG('Key ammo switch error:', str(e))
						
				if event.isKeyDown() and event.key in [Keys.KEY_4, Keys.KEY_5, Keys.KEY_6]:
					try:
						import BigWorld
						player = BigWorld.player()
						idx_map = {Keys.KEY_4: 3, Keys.KEY_5: 4, Keys.KEY_6: 5}
						slot_idx = idx_map[event.key]
						for cons in _gun_state.get('consumables', []):
							if cons['slot'] == slot_idx and not cons['used']:
								tag = cons['tag']
								used = False
								if tag == 'extinguisher' and getattr(_player_mock, 'is_on_fire', False):
									_player_mock.is_on_fire = False
									import gui.WindowsManager
									bw = gui.WindowsManager.g_windowsManager.battleWindow
									if hasattr(bw, 'damagePanel'):
										bw.damagePanel._DamagePanel__callFlash('onFireInVehicle', [False])
									used = True
								elif tag == 'repairkit':
									# Repair all modules
									_player_mock.is_tracked = False
									try:
										import gui.WindowsManager
										bw = gui.WindowsManager.g_windowsManager.battleWindow
										if hasattr(bw, 'damagePanel'):
											for ui_name in ('engine', 'ammoBay', 'fuelTank', 'radio', 'leftTrack', 'rightTrack', 'track', 'gun', 'turretRotator', 'surveyingDevice'):
												try: bw.damagePanel.updateDeviceState(ui_name, 'normal')
												except: pass
									except: pass
									used = True
								elif tag == 'medkit':
									# Heal all crew
									try:
										import gui.WindowsManager
										bw = gui.WindowsManager.g_windowsManager.battleWindow
										if hasattr(bw, 'damagePanel'):
											for ui_name in ('commander', 'driver', 'radioman1', 'radioman2', 'gunner1', 'gunner2', 'loader1', 'loader2'):
												try: bw.damagePanel.updateDeviceState(ui_name, 'normal')
												except: pass
									except: pass
									used = True
								
								if used:
									cons['used'] = True
									try:
										import gui.WindowsManager
										panel = gui.WindowsManager.g_windowsManager.battleWindow.consumablesPanel
										if panel:
											panel.setItemQuantityInSlot(slot_idx, 0)
											panel.setCoolDownTime(slot_idx, -1)
									except: pass
								break
					except Exception as e:
						import debug_utils
						debug_utils.LOG_DEBUG('Consumable hotkey error:', str(e))

				if event.isKeyDown() and event.key == Keys.KEY_K:
					try:
						import BigWorld
						player = BigWorld.player()
						if hasattr(player, 'arena'):
							p_team = getattr(player, '_offhangar_team', getattr(player, 'team', 1))
							p_name = getattr(player, 'name', 'Player')
							p_dbid = getattr(player, 'databaseID', 1)
							_td = None
							try: _td = loaded_models.get('td')
							except: pass
							if not _td: _td = getattr(player, 'vehicleTypeDescriptor', None)
							
							p_cd = getattr(getattr(_td, 'type', None), 'compactDescr', 0)
							
							LOG_DEBUG('BATTLE RESULTS LOCAL P_CD IS:', p_cd)
							
							import debug_utils
							debug_utils.LOG_DEBUG('BATTLE RESULTS P_CD IS:', p_cd)
							
							if p_cd == 0 and hasattr(player, 'arena') and player.playerVehicleID in player.arena.vehicles:
								_vinfo = player.arena.vehicles[player.playerVehicleID]
								_vtype = _vinfo.get('vehicleType', None)
								if _vtype:
									p_cd = getattr(getattr(_vtype, 'type', None), 'compactDescr', 0)
									debug_utils.LOG_DEBUG('BATTLE RESULTS FALLBACK P_CD IS:', p_cd)
							
							allied = sum(v.get('frags', 0) for v in player.arena.vehicles.values() if v.get('team', 2) == p_team)
							enemy = sum(v.get('frags', 0) for v in player.arena.vehicles.values() if v.get('team', 2) != p_team)
							
							def _show_res():
								try:
									from gui.SystemMessages import SM_TYPE, pushMessage
									pushMessage('Offline battle finished. Returning to Hangar...'.encode('utf-8'), SM_TYPE.Information)
								except Exception as e: LOG_DEBUG('CRITICAL ERROR IN K KEY:', e); import traceback; LOG_DEBUG(traceback.format_exc())
								
								try:
									import MusicController
									if hasattr(MusicController, 'g_musicController') and MusicController.g_musicController:
										_mc = MusicController.g_musicController
										try: _mc.stop()
										except: pass
										evt = None
										if allied > enemy:
											evt = getattr(MusicController, 'MUSIC_EVENT_COMBAT_VICTORY', getattr(MusicController, 'MUSIC_EVENT_VICTORY', 'music_victory'))
										elif allied < enemy:
											evt = getattr(MusicController, 'MUSIC_EVENT_COMBAT_LOSE', getattr(MusicController, 'MUSIC_EVENT_LOSE', 'music_lose'))
										else:
											evt = getattr(MusicController, 'MUSIC_EVENT_COMBAT_DRAW', getattr(MusicController, 'MUSIC_EVENT_DRAW', 'music_draw'))
										try: _mc.play(evt)
										except: pass
								except Exception as e: LOG_DEBUG('CRITICAL ERROR IN K KEY MUSIC:', e); import traceback; LOG_DEBUG(traceback.format_exc())
								
								try:
									import battle_results_shared
									mock_arena_id = 999
									
									v_id = getattr(player, 'playerVehicleID', 1)
									p_max_health = getattr(getattr(player, 'vehicleTypeDescriptor', None), 'maxHealth', 1000)
									p_health = getattr(getattr(player, 'vehicle', None), 'health', p_max_health)
									
									_player_mock = globals().get('G_MOCK_VEHICLES', {}).get(getattr(player, 'playerVehicleID', -1))
									_p_killer_id = getattr(_player_mock, 'last_killer_id', 255) if p_health <= 0 else 0
									
									total_dmg_dealt = 0
									total_frags = 0
									total_hits = 0
									players_dict = {p_dbid: {'name': p_name, 'clanDBID': 0, 'clanAbbrev': '', 'prebattleID': 0, 'team': p_team, 'igrType': 0}}
									vehicles_dict = {v_id: {'health': p_health, 'credits': 10000, 'xp': 1000, 'shots': 10, 'hits': 8, 'he_hits': 0, 'pierced': 8, 'damageDealt': 0, 'damageAssisted': 0, 'damageReceived': max(0, p_max_health - p_health), 'shotsReceived': 0, 'spotted': 0, 'damaged': 0, 'kills': 0, 'tdamageDealt': 0, 'tkills': 0, 'isTeamKiller': False, 'capturePoints': 0, 'droppedCapturePoints': 0, 'mileage': 100, 'lifeTime': 300, 'killerID': _p_killer_id, 'achievements': [], 'repair': 0, 'freeXP': 50, 'details': {}, 'accountDBID': p_dbid, 'team': p_team, 'typeCompDescr': p_cd, 'gold': 0}}
									personal_details = {}
									
									for vid, vinfo in getattr(player.arena, 'vehicles', {}).items():
										if vid == v_id: continue
										bot_team = vinfo.get('team', 2)
										
										_mock_vehicles = globals().get('G_MOCK_VEHICLES', {})
										if vid in _mock_vehicles:
											bot_team = getattr(_mock_vehicles[vid], '_bot_team', bot_team)
										bot_name = vinfo.get('name', 'Bot')
										# Force bot DBID to be its vehicle ID so it never overlaps the player's DBID!
										bot_dbid = vid
										td = vinfo.get('vehicleType', None)
										
										_mock_vehicles = globals().get('G_MOCK_VEHICLES', {})
										if vid in _mock_vehicles:
											_true_td = getattr(_mock_vehicles[vid], 'typeDescriptor', None)
											if _true_td: td = _true_td
										
										td_type = getattr(td, 'type', None)
										bot_cd = getattr(td_type, 'compactDescr', 0)
										
										players_dict[bot_dbid] = {'name': bot_name, 'clanDBID': 0, 'clanAbbrev': '', 'prebattleID': 0, 'team': bot_team, 'igrType': 0}
										
										is_killed = not vinfo.get('isAlive', True)
										bot_hp = getattr(td, 'maxHealth', 1000)
										bot_max_hp = bot_hp
										
										_mock_vehicles = globals().get('G_MOCK_VEHICLES', {})
										if vid in _mock_vehicles:
											bot_hp = max(0, getattr(_mock_vehicles[vid], 'health', 0))
											bot_max_hp = getattr(_mock_vehicles[vid], 'maxHealth', bot_max_hp)
											if bot_hp <= 0: is_killed = True
										
										if not 'mock_vehicles' in locals() and not '_mock_vehicles' in locals():
											bot_hp = 0 if is_killed else bot_max_hp
											
										# Retrieve damage tracking from mock_vehicles
										_dmg_from_player = 0
										_dmg_from_bots = 0
										_hits_from_player = 0
										_mock_vehicles = globals().get('G_MOCK_VEHICLES', {})
										if vid in _mock_vehicles:
											_dmg_from_player = getattr(_mock_vehicles[vid], 'damage_from_player', 0)
											_dmg_from_bots = getattr(_mock_vehicles[vid], 'damage_from_bots', 0)
											_hits_from_player = getattr(_mock_vehicles[vid], 'hits_from_player', 0)
										
										# Removed dangerous fallback! Only explicitly tracked damage counts.
										dmg_received = bot_max_hp - bot_hp
										
										player_killed_this = is_killed and _dmg_from_player > 0 and _dmg_from_player >= (dmg_received / 2.0)
										if player_killed_this and bot_team == p_team: player_killed_this = False
										
										total_dmg_dealt += _dmg_from_player
										total_hits += _hits_from_player
										if player_killed_this: total_frags += 1
										
										killer_id = v_id if player_killed_this else (getattr(_mock_vehicles.get(vid, None), 'last_killer_id', 255) if is_killed else 0)
										
										# Simulate some random shots and hits if the bot dealt damage
										_bot_shots = max(1, int(_dmg_from_bots / 200.0)) if _dmg_from_bots > 0 else 1
										vehicles_dict[vid] = {'health': bot_hp, 'credits': 100, 'xp': 100, 'shots': _bot_shots, 'hits': _bot_shots, 'he_hits': 0, 'pierced': _bot_shots, 'damageDealt': _dmg_from_bots, 'damageAssisted': 0, 'damageReceived': dmg_received, 'shotsReceived': max(1, int(dmg_received / 300.0)) if dmg_received > 0 else 0, 'spotted': 0, 'damaged': 0, 'kills': 0, 'tdamageDealt': 0, 'tkills': 0, 'isTeamKiller': False, 'capturePoints': 0, 'droppedCapturePoints': 0, 'mileage': 100, 'lifeTime': 300, 'killerID': killer_id, 'achievements': [], 'repair': 0, 'freeXP': 5, 'details': {}, 'accountDBID': bot_dbid, 'team': bot_team, 'typeCompDescr': bot_cd, 'gold': 0}
										
										if _dmg_from_player > 0 or bot_team != p_team:
											personal_details[vid] = {'spotted': 1 if bot_team != p_team else 0, 'killed': 1 if player_killed_this else 0, 'hits': _hits_from_player, 'he_hits': 0, 'pierced': _hits_from_player, 'damageDealt': _dmg_from_player, 'damageAssisted': 0, 'crits': 1 if player_killed_this else 0, 'fire': 0}
											
									vehicles_dict[v_id]['damageDealt'] = total_dmg_dealt
									vehicles_dict[v_id]['kills'] = 0 # Will be populated by the loop below
									vehicles_dict[v_id]['hits'] = total_hits
									vehicles_dict[v_id]['pierced'] = total_hits
									vehicles_dict[v_id]['shots'] = max(10, total_hits + 2)
									vehicles_dict[v_id]['spotted'] = len(personal_details)
									vehicles_dict[v_id]['damaged'] = len(personal_details)
									
									for v_iter_id, v_iter_data in vehicles_dict.items():
										k_id = v_iter_data.get('killerID', 0)
										if k_id and k_id in vehicles_dict and k_id != v_iter_id:
											vehicles_dict[k_id]['kills'] = vehicles_dict[k_id].get('kills', 0) + 1
									
									mock_res = {
										'arenaUniqueID': mock_arena_id,
										'personal': {'health': p_health, 'credits': 10000, 'xp': 1000, 'shots': globals().get('G_OFFHANGAR_SHOTS_FIRED', max(0, total_hits)), 'hits': total_hits, 'he_hits': 0, 'pierced': total_hits, 'damageDealt': total_dmg_dealt, 'damageAssisted': 0, 'damageReceived': 0, 'shotsReceived': 0, 'spotted': len(personal_details), 'damaged': len(personal_details), 'kills': total_frags, 'tdamageDealt': 0, 'tkills': 0, 'isTeamKiller': False, 'capturePoints': 0, 'droppedCapturePoints': 0, 'mileage': 100, 'lifeTime': 300, 'killerID': _p_killer_id, 'achievements': [], 'repair': 0, 'freeXP': 50, 'details': personal_details, 'accountDBID': p_dbid, 'team': p_team, 'typeCompDescr': p_cd, 'gold': 0, 'xpPenalty': 0, 'creditsPenalty': 0, 'creditsContributionIn': 0, 'creditsContributionOut': 0, 'tmenXP': 0, 'eventCredits': 0, 'eventGold': 0, 'eventXP': 0, 'eventFreeXP': 0, 'eventTMenXP': 0, 'autoRepairCost': 0, 'autoLoadCost': (0, 0), 'autoEquipCost': (0, 0), 'isPremium': True, 'premiumXPFactor10': 15, 'premiumCreditsFactor10': 15, 'dailyXPFactor10': 10, 'aogasFactor10': 10, 'markOfMastery': 0, 'dossierPopUps': []},
										'common': {'arenaTypeID': getattr(player.arena, 'arenaTypeID', 1), 'arenaCreateTime': __import__('time').time(), 'winnerTeam': p_team if allied > enemy else (0 if allied==enemy else (3-p_team)), 'finishReason': 1, 'duration': 300, 'bonusType': 1, 'guiType': 1, 'vehLockMode': 0},
										'players': players_dict,
										'vehicles': vehicles_dict
									}
									
									if hasattr(battle_results_shared, 'VEH_FULL_RESULTS'):
										for k in battle_results_shared.VEH_FULL_RESULTS:
											if k not in mock_res['personal']: mock_res['personal'][k] = [] if 'list' in k or k == 'achievements' else (0 if k != 'details' else {})
									if hasattr(battle_results_shared, 'VEH_BASE_RESULTS'):
										for k in battle_results_shared.VEH_BASE_RESULTS:
											for v in mock_res['vehicles']:
												if k not in mock_res['vehicles'][v]: mock_res['vehicles'][v][k] = [] if 'list' in k or k == 'achievements' else (0 if k != 'details' else {})
									
									def _mock_get(arenaUniqueID, callback):
										import BigWorld
										BigWorld.callback(0.1, lambda: callback(1, mock_res))
									
									player_brc = getattr(player, 'battleResultsCache', None)
									if player_brc:
										orig_br_get = player_brc.get
										player_brc.get = _mock_get
									
									from gui import WindowsManager
									window = getattr(WindowsManager.g_windowsManager, 'window', None)
									if hasattr(window, 'onBattleResultsReceived'): window.onBattleResultsReceived(True, mock_arena_id)
									elif hasattr(window, 'battleResults') and hasattr(window.battleResults, 'show'): window.battleResults.show(mock_arena_id)
									elif hasattr(window, 'battleResults') and hasattr(window.battleResults, '_BattleResultsManager__showBattleResults'): window.battleResults._BattleResultsManager__showBattleResults(mock_arena_id)
								except Exception as e: LOG_DEBUG('CRITICAL ERROR IN K KEY:', e); import traceback; LOG_DEBUG(traceback.format_exc())
							
							BigWorld.callback(4.0, _show_res)
							player.leaveArena()
					except Exception: pass

				if event.isKeyDown() and event.key in (Keys.KEY_O, Keys.KEY_P, Keys.KEY_L):
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
						bot_team = 1 if event.key == Keys.KEY_L else 2
						if event.key == Keys.KEY_L: bot_name = 'Ally ' + str(_spawn_count[0])
						if event.key == Keys.KEY_O:
							td = loaded_models.get('td')
							bot_name = 'Clone ' + str(_spawn_count[0])
						elif event.key in (Keys.KEY_P, Keys.KEY_L):
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
									bot_name = ('Ally ' if bot_team == 1 else 'Enemy ') + chosen.split(':')[-1] + ' ' + str(_spawn_count[0])
							except Exception as e:
								import traceback
								LOG_DEBUG('Random spawn error:', str(e), traceback.format_exc())
								td = loaded_models.get('td')
						
						if not td: return True
						
						try:
							for hitTester in td.getHitTesters():
								hitTester.loadBspModel()
						except Exception as e:
							LOG_DEBUG("Error loading hitTesters for bot:", str(e))
						
						e_id = 1000 + _spawn_count[0]
						_spawn_count[0] += 1
						
						# Load visual models
						
						def _on_bot_models_loaded(resourceRefs):
							try:
								ch = resourceRefs[td.chassis['models']['undamaged']]
								hu = resourceRefs[td.hull['models']['undamaged']]
								tu = resourceRefs[td.turret['models']['undamaged']]
								gu = resourceRefs[td.gun['models']['undamaged']]
							except Exception as e:
								import debug_utils
								debug_utils.LOG_DEBUG('Bot model unpack error:', str(e))
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
							e_mock._bot_team = bot_team
							LOG_DEBUG('SPAWN BOT: bot_team=%s bot_name=%s player_team=%s' % (bot_team, bot_name, getattr(player, '_offhangar_team', -99)))
							e_mock.publicInfo = {
								'vehicleType': td,
								'name': bot_name,
								'team': bot_team,
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
							
							_eid = BigWorld.createEntity('OfflineEntity', player.spaceID, 0, e_mock.position, (0, 0, e_mock.yaw), dict())
							e_mock.bw_entity = None
							def _assign_model_when_ready(eid, model_to_add, retries=10, _e_mock=e_mock):
								ent = BigWorld.entity(eid)
								if ent:
									ent.model = model_to_add  # Outline needs it!
									try:
										ent.filter = BigWorld.AvatarFilter()
									except: pass
									_e_mock.bw_entity = ent
								elif retries > 0:
									BigWorld.callback(0.1, lambda: _assign_model_when_ready(eid, model_to_add, retries - 1, _e_mock))
								else:
									_add_model(model_to_add)
							_assign_model_when_ready(_eid, ch)
							h_mat = Math.Matrix(); h_mat.setIdentity()
							t_mat = Math.Matrix(); t_mat.setIdentity()
							g_mat = Math.Matrix(); g_mat.setIdentity()
							ch.node('V').attach(hu)
							e_mock._t_node = hu.node('HP_turretJoint', t_mat)
							e_mock._t_node.attach(tu)
							e_mock._g_node = tu.node('HP_gunJoint', g_mat)
							e_mock._g_node.attach(gu)
							e_mock.model = ch
							e_mock.typeDescriptor = td
							e_mock._chassis_model = ch
							e_mock._hull_model = hu
							e_mock._turret_model = tu
							e_mock._gun_model = gu
							e_mock._t_mat = t_mat
							try:
								e_mock._collision_obstacle = BigWorld.PyModelObstacle(
									td.hull['models']['undamaged'],
									td.turret['models']['undamaged'],
									e_mock.matrix,
									True
								)
							except Exception as e:
								LOG_DEBUG('OfflineBattle PyModelObstacle Error:', e)
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
								bw = getattr(WindowsManager.g_windowsManager, 'battleWindow', None)
								if bw and hasattr(bw, '_Battle__updatePlayers'):
									bw._Battle__updatePlayers()
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
						
						BigWorld.loadResourceListBG((
							td.chassis['models']['undamaged'],
							td.hull['models']['undamaged'],
							td.turret['models']['undamaged'],
							td.gun['models']['undamaged'],
						), _on_bot_models_loaded)
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
			
			# ---- ZVUK: okamžitě zastavit garážové audio, spustit loading hudbu ----
			try:
				import MusicController as _MC
				
				
				if not hasattr(_MC, '_orig_play'):
					_MC._orig_play = _MC.MusicController.play
					def _mock_play(self, eventName):
						from debug_utils import LOG_DEBUG
						import traceback
						LOG_DEBUG('MusicController.play called with:', eventName)
						LOG_DEBUG('Traceback:', ''.join(traceback.format_stack()))
						return _MC._orig_play(self, eventName)
					_MC.MusicController.play = _mock_play
				if not hasattr(_MC, '_orig_stopMusic'):
					_MC._orig_stopMusic = _MC.MusicController.stopMusic
					def _mock_stopMusic(self, *args, **kwargs):
						from debug_utils import LOG_DEBUG
						import traceback
						LOG_DEBUG('MusicController.stopMusic called!')
						LOG_DEBUG('Traceback:', ''.join(traceback.format_stack()))
						return _MC._orig_stopMusic(self, *args, **kwargs)
				_mc = _MC.g_musicController
				try:
					import SoundGroups as _SG
					if getattr(_SG, 'g_instance', None) is not None:
						_SG.g_instance.setVolume('music', 1.0)
						_SG.g_instance.setVolume('ambient', 1.0)
				except Exception: pass
				
				# 1) Okamžitě zastavit staré FMOD sound eventy
				_snd_music = getattr(_mc, '_MusicController__sndEventMusic', None)
				if _snd_music is not None:
					try: _snd_music.stop()
					except Exception: pass
				_snd_ambient = getattr(_mc, '_MusicController__sndEventAmbient', None)
				if _snd_ambient is not None:
					try: _snd_ambient.stop()
					except Exception: pass
				
				# 2) Zastavit interní stav
				_mc.stopAmbient()
				_mc.stopMusic()
				
				# 3) Aplikovat patch přímo na instanci
				def _mock_mc_getArenaSoundEvent(self, eventId):
					from debug_utils import LOG_DEBUG
					import BigWorld
					player = BigWorld.player()
					if hasattr(player, 'arena') and hasattr(player.arena, 'arenaType'):
						# 1. Camouflage
						import items.vehicles as iv
						cust = iv.g_cache.customization(td.type.id[0])
						camo_kind = getattr(player.arena.arenaType, 'vehicleCamouflageKind', 0)
						camo_params = td.camouflages[camo_kind] if len(td.camouflages) > camo_kind else None
						LOG_DEBUG('OfflineBattle.customization:', 'kind', camo_kind, 'params', camo_params, 'emblems', td.playerEmblems)
						sound_name = ''
						if eventId == _MC.MUSIC_EVENT_COMBAT:
							sound_name = getattr(player.arena.arenaType, 'music', '')
						elif eventId == _MC.MUSIC_EVENT_COMBAT_LOADING:
							sound_name = getattr(player.arena.arenaType, 'loadingMusic', '')
						elif eventId == _MC.AMBIENT_EVENT_COMBAT:
							sound_name = getattr(player.arena.arenaType, 'ambientSound', '')
						LOG_DEBUG('OfflineBattle.mock_getArenaSoundEvent DIRECT', eventId, sound_name)
						if sound_name:
							import FMOD
							return FMOD.getSound(sound_name)
					return _MC.MusicController._MusicController__getArenaSoundEvent(self, eventId)

				import types
				_mc._MusicController__getArenaSoundEvent = types.MethodType(_mock_mc_getArenaSoundEvent, _mc)
				
				# 3) Spustit loading hudbu pro bitvu
				_mc.play(_MC.MUSIC_EVENT_COMBAT_LOADING)
				LOG_DEBUG('OfflineBattle.sounds.battle_start', 'COMBAT_LOADING OK')
			except Exception as _se:
				LOG_DEBUG('OfflineBattle.sounds.battle_start error', _se)
			# ---- konec zvuk ----
			
			WindowsManager.g_windowsManager.startBattle()
			WindowsManager.g_windowsManager.showBattleLoading()
			
			if hasattr(player, 'arena'):
				if hasattr(player.arena, 'onVehicleAdded'):
					for vID in player.arena.vehicles.keys():
						player.arena.onVehicleAdded(vID)
				
				def _finish_battle_load():
					try:
						try:
							import SoundGroups as _SG
							if getattr(_SG, 'g_instance', None) is not None:
								_SG.g_instance.enableLobbySounds(False)
								_SG.g_instance.enableArenaSounds(True)
							import MusicController as _MC
							_MC.g_musicController.play(_MC.MUSIC_EVENT_COMBAT)
							from debug_utils import LOG_DEBUG
							LOG_DEBUG('OfflineBattle.sounds', 'MUSIC_EVENT_COMBAT triggered in finish load')
						except Exception as e: pass
						
						Waiting.close()
						WindowsManager.g_windowsManager.showBattle()
						BigWorld.worldDrawEnabled(True)
						
						import AvatarInputHandler.cameras
						AvatarInputHandler.cameras.SniperCamera._USE_SWINGING = False
						BigWorld.wg_isSniperModeSwingingEnabled = lambda *a, **kw: False
						
						if not hasattr(BigWorld, '_orig_serverTime'):
							BigWorld._orig_serverTime = BigWorld.serverTime
							BigWorld._offline_start_time = __import__('time').time()
							def _mock_serverTime():
								return __import__('time').time() - BigWorld._offline_start_time
							BigWorld.serverTime = _mock_serverTime
						
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
									_battle_duration = 900
									
									# RESTORE PREBATTLE
									player.arena.period = 2
									player.arena.periodLength = 15
									player.arena.periodEndTime = BigWorld.serverTime() + 15
									player.arena.onPeriodChange(2, player.arena.periodEndTime, 15, 0)
									try:
										import MusicController as _MC
										_MC.g_musicController.play(_MC.MUSIC_EVENT_NONE)
									except: pass
									
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
											except Exception as e: LOG_DEBUG('CRITICAL ERROR IN K KEY:', e); import traceback; LOG_DEBUG(traceback.format_exc())
											
										try:
											g_offline_aih._AvatarInputHandler__isSPG = 'SPG' in td.type.tags
										except Exception:
											g_offline_aih._AvatarInputHandler__isSPG = False

										g_offline_aih.onControlModeChanged('arcade')
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
		ctx = build_offline_battle_context(player, vehInvID, cmdName)
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
				arena.arenaTypeID = map_id
				try:
					import ArenaType
					if hasattr(ArenaType, 'g_cache') and isinstance(ArenaType.g_cache, dict):
						for k, v in ArenaType.g_cache.iteritems():
							if v is real_arena_type:
								arena.arenaTypeID = k
								break
				except Exception: pass
				LOG_DEBUG('OfflineBattle.arenaType.real', map_name, 'arenaTypeID', arena.arenaTypeID, 'geomName', getattr(real_arena_type, 'geometryName', ''), 'minimap', hasattr(real_arena_type, 'minimap'))
			elif getattr(arena, 'arenaType', None) is not None:
				# Fallback: keep stub, but ensure required attrs exist.
				arena.arenaTypeID = map_id
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
	import debug_utils
	try:
		from gui.battle_control import constants as bc_constants
		debug_utils.LOG_DEBUG("VEHICLE_VIEW_STATE:", dir(bc_constants.VEHICLE_VIEW_STATE))
		for k in dir(bc_constants.VEHICLE_VIEW_STATE):
			if not k.startswith('_'): debug_utils.LOG_DEBUG("VIEW_STATE", k, getattr(bc_constants.VEHICLE_VIEW_STATE, k))
	except Exception as e: debug_utils.LOG_DEBUG("DUMP ERR1", e)
	try:
		import constants
		debug_utils.LOG_DEBUG("VEHICLE_DEVICE_STATES:", dir(constants.VEHICLE_DEVICE_STATES))
		for k in dir(constants.VEHICLE_DEVICE_STATES):
			if not k.startswith('_'): debug_utils.LOG_DEBUG("DEV_STATE", k, getattr(constants.VEHICLE_DEVICE_STATES, k))
	except Exception as e: debug_utils.LOG_DEBUG("DUMP ERR2", e)
	
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

try:
	import gui.Scaleform.battledispatcherinterface as bdi
	if hasattr(bdi, 'BattleDispatcherInterface'):
		orig_updateFightButton = bdi.BattleDispatcherInterface.updateFightButton
		def _new_updateFightButton(self):
			orig_updateFightButton(self)
			
			fightTypes = getattr(self, '_offhangar_fightTypes_temp', None)
			if fightTypes is None:
				# In case we can't capture it easily, we just call self.call again!
				pass
		
		# Better approach: monkey-patch self.call in BattleDispatcherInterface
		orig_call = bdi.BattleDispatcherInterface.call
		def _new_call(self, methodName, args=None):
			from gui.mods.offhangar.logging import LOG_DEBUG
			LOG_DEBUG("FLASH CALL:", methodName, args)
			if methodName == 'common.setFightButton' and isinstance(args, list):
				args.append('Bootcamp')
				args.append('tutorial')
				args.append(False)
				args.append('')
			return orig_call(self, methodName, args)
		bdi.BattleDispatcherInterface.call = _new_call

		orig_onFightButtonClick = bdi.BattleDispatcherInterface.onFightButtonClick
		def _new_onFightButtonClick(self, callbackId, mapId=None, queueType=0, confirm=False):
			import BigWorld
			p = BigWorld.player()
			from gui.mods.offhangar.logging import LOG_DEBUG
			LOG_DEBUG("FIGHT BUTTON CLICKED", "mapId:", mapId, "type:", type(mapId), "queueType:", queueType, "type:", type(queueType))
			
			if queueType == 'tutorial':
				if hasattr(p, 'enqueueTutorial'):
					p.enqueueTutorial()
				return
			
			if queueType == 'demonstrator':
				if mapId is not None:
					setattr(p, '_offhangar_selected_mapId', mapId)
				if hasattr(self, 'respond'):
					try: self.respond(callbackId, True)
					except: pass
				start_offline_random_from_hangar(p, 0)
				return
			
			# If it's a regular random battle, ensure we clear any demonstrator map override!
			if hasattr(p, '_offhangar_selected_mapId'):
				delattr(p, '_offhangar_selected_mapId')
				
			return orig_onFightButtonClick(self, callbackId, mapId, queueType, confirm)
		bdi.BattleDispatcherInterface.onFightButtonClick = _new_onFightButtonClick
except Exception:
	import traceback
	LOG_DEBUG('Failed to hook UI')
	LOG_DEBUG(traceback.format_exc())
