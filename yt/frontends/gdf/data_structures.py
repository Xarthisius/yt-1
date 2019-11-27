"""Data structures for GDF."""

#-----------------------------------------------------------------------------
# Copyright (c) 2013, yt Development Team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file COPYING.txt, distributed with this software.
#-----------------------------------------------------------------------------

from yt.utilities.on_demand_imports import _h5py as h5py
import numpy as np
import weakref
import os
from yt.funcs import \
    ensure_tuple, \
    just_one, \
    setdefaultattr
from yt.data_objects.grid_patch import \
    AMRGridPatch
from yt.geometry.grid_geometry_handler import \
    GridIndex
from yt.data_objects.static_output import \
    Dataset
from yt.units.dimensions import \
    dimensionless as sympy_one
from yt.units.unit_object import \
    Unit
from yt.units.unit_systems import \
    unit_system_registry
from yt.utilities.exceptions import \
    YTGDFUnknownGeometry
from yt.utilities.lib.misc_utilities import \
    get_box_grids_level
from yt.utilities.logger import ytLogger as mylog
from .fields import GDFFieldInfo
from .io import _grid_dname

GEOMETRY_TRANS = {
    0: "cartesian",
    1: "polar",
    2: "cylindrical",
    3: "spherical",
}


class GDFGrid(AMRGridPatch):
    _id_offset = 0

    def __init__(self, id, index, level, start, dimensions):
        AMRGridPatch.__init__(self, id, filename=index.index_filename,
                              index=index)
        self.Parent = []
        self.Children = []
        self.Level = level
        self.start_index = start.copy()
        self.stop_index = self.start_index + dimensions
        self.ActiveDimensions = dimensions.copy()

    def _setup_dx(self):
        # So first we figure out what the index is.  We don't assume
        # that dx=dy=dz , at least here.  We probably do elsewhere.
        id = self.id - self._id_offset
        if len(self.Parent) > 0:
            self.dds = self.Parent[0].dds / self.ds.refine_by
        else:
            LE, RE = self.index.grid_left_edge[id, :], \
                self.index.grid_right_edge[id, :]
            self.dds = np.array((RE - LE) / self.ActiveDimensions)
        if self.ds.data_software != "piernik":
            if self.ds.dimensionality < 2:
                self.dds[1] = 1.0
            if self.ds.dimensionality < 3:
                self.dds[2] = 1.0
        self.field_data['dx'], self.field_data['dy'], self.field_data['dz'] = \
            self.dds
        self.dds = self.ds.arr(self.dds, "code_length")


class GDFHierarchy(GridIndex):

    grid = GDFGrid

    def __init__(self, ds, dataset_type='grid_data_format'):
        self.dataset = weakref.proxy(ds)
        self.index_filename = self.dataset.parameter_filename
        self.dataset_type = dataset_type
        self.directory = os.path.dirname(self.index_filename)

        with h5py.File(self.index_filename, 'r') as self._index_handle:
            GridIndex.__init__(self, ds, dataset_type)

    def _detect_output_fields(self):
        """
        Set up available fields.

        Called during:
            1. self.__init__ ->
            2. Index.__init__
        """
        # Set handy aliases
        field_info = self.ds._field_info_class
        h5f = self._index_handle

        defined_fields = set(
            str(name) for name in h5f["field_types"].keys()
        )
        known_other_fields = {
            field[0] for field in field_info.known_other_fields
        }
        defined_fields = defined_fields.union(known_other_fields)
        on_disk_fields = set(
            str(name) for name in h5f[_grid_dname(0)].keys()
        )
        valid_fields = defined_fields.intersection(on_disk_fields)

        self.field_list = [("gdf", field) for field in valid_fields]
        if self.grid_particle_count.sum() < 1:
            return

        for ptype in self.ds.particle_types:
            pfield_attrs = "/particle_types/{}".format(ptype)
            if pfield_attrs:
                defined_particle_fields = set(
                    str(name)
                    for name in h5f.get("/particle_types/{}".format(ptype), {}).keys()
                )
            else:
                defined_particle_fields = {}

            known_particle_fields = {
                field[0] for field in field_info.known_particle_fields
            }
            defined_particle_fields = defined_particle_fields.union(
                known_particle_fields
            )
            self.field_list += [(ptype, field) for field in defined_particle_fields]

    def _count_grids(self):
        """
        Set up grids count.

        Called during:
            1. self.__init__ ->
            2. Index.__init__ ->
            3. GridIndex._setup_geometry
        Before: _detect_output_fields
        """
        self.num_grids = self._index_handle['/grid_parent_id'].shape[0]

    def _parse_index(self):
        """
        Set up basic grids' properties.

        Called during:
            1. self.__init__ ->
            2. Index.__init__ ->
            3. GridIndex._setup_geometry
        Before: _detect_output_fields
        After: _count_grids
        """
        h5f = self._index_handle
        dxs = []
        self.grids = np.empty(self.num_grids, dtype='object')
        levels = (h5f['grid_level'][:]).copy()
        glis = (h5f['grid_left_index'][:]).copy()
        gdims = (h5f['grid_dimensions'][:]).copy()
        active_dims = ~((np.max(gdims, axis=0) == 1) &
                        (self.dataset.domain_dimensions == 1))

        for i in range(levels.shape[0]):
            self.grids[i] = self.grid(i, self, levels[i],
                                      glis[i],
                                      gdims[i])
            self.grids[i]._level_id = levels[i]

            dx = (self.dataset.domain_right_edge -
                  self.dataset.domain_left_edge) / \
                self.dataset.domain_dimensions
            dx[active_dims] /= self.dataset.refine_by ** levels[i]
            dxs.append(dx.in_units("code_length"))
        dx = self.dataset.arr(dxs, input_units="code_length")
        self.grid_left_edge = self.dataset.domain_left_edge + dx * glis
        self.grid_dimensions = gdims.astype("int32")
        self.grid_right_edge = self.grid_left_edge + dx * self.grid_dimensions
        self.grid_particle_count = h5f['grid_particle_count'][:]
        del levels, glis, gdims

    def _populate_grid_objects(self):
        """
        Set up addtional grids' properties.

        Called during:
            1. self.__init__ ->
            2. Index.__init__ ->
            3. GridIndex._setup_geometry
        Before: _detect_output_fields
        After: _count_grids, _parse_index
        """
        mask = np.empty(self.grids.size, dtype='int32')
        for gi, g in enumerate(self.grids):
            g._prepare_grid()
            g._setup_dx()

        for gi, g in enumerate(self.grids):
            g.Children = self._get_grid_children(g)
            for g1 in g.Children:
                g1.Parent.append(g)
            get_box_grids_level(self.grid_left_edge[gi, :],
                                self.grid_right_edge[gi, :],
                                self.grid_levels[gi],
                                self.grid_left_edge, self.grid_right_edge,
                                self.grid_levels, mask)
            m = mask.astype("bool")
            m[gi] = False
            siblings = self.grids[gi:][m[gi:]]
            if len(siblings) > 0:
                g.OverlappingSiblings = siblings.tolist()
        self.max_level = self.grid_levels.max()

    def _get_box_grids(self, left_edge, right_edge):
        """
        Get back all the grids between a left edge and right edge.

        Note: used only in self._get_grid_children
        """
        eps = np.finfo(np.float64).eps
        grid_i = np.where(
            np.all((self.grid_right_edge - left_edge) > eps, axis=1) &
            np.all((right_edge - self.grid_left_edge) > eps, axis=1))

        return self.grids[grid_i], grid_i


    def _get_grid_children(self, grid):
        """
        Get child grids of the current grid.

        Note: used only in self._populate_grid_objects
        """
        mask = np.zeros(self.num_grids, dtype='bool')
        grids, grid_ind = self._get_box_grids(grid.LeftEdge, grid.RightEdge)
        mask[grid_ind] = True
        return [g for g in self.grids[mask] if g.Level == grid.Level + 1]


class GDFDataset(Dataset):
    _index_class = GDFHierarchy
    _field_info_class = GDFFieldInfo

    def __init__(self, filename, dataset_type='grid_data_format',
                 storage_filename=None, geometry=None,
                 units_override=None, unit_system="cgs"):
        self.geometry = geometry
        self.fluid_types += ("gdf",)
        Dataset.__init__(self, filename, dataset_type,
                         units_override=units_override, unit_system=unit_system)
        self.storage_filename = storage_filename
        self.filename = filename

    @staticmethod
    def _extract_units_from_attrs(field):
        """Parse field unit definition into Unit.

        GDF defines 3 attributes describing unit fields:
          * field_to_cgs - convertion factor to CGS
          * field_units - string with units
          * field_name - human readable field name
        """
        field_conv = just_one(field.attrs.get("field_to_cgs", 1.0))
        field_units = just_one(field.attrs.get("field_units", ""))
        if field_units:
            if field_conv == 1.0:  # I hate float comparison...
                return field_units.decode()
            else:
                return "{} * {}".format(field_conv, field_units.decode())
        else:
            return field_conv

    def _set_code_unit_attributes(self):
        """
        Generate the conversion to various physical _units based on the parameter file.
        """
        h5f = h5py.File(self.parameter_filename, "r")
        for field_name in h5f["/field_types"]:
            current_field = h5f["/field_types/%s" % field_name]
            self.field_units[field_name] = self._extract_units_from_attrs(current_field)

        for ptype in h5f.get("/particle_types", []):
            for field_name in h5f["/particle_types"][ptype]:
                current_field = h5f[os.path.join("/particle_types", ptype, field_name)]
                pfield_name = (ptype, field_name)
                self.field_units[pfield_name] = self._extract_units_from_attrs(current_field)

        if "dataset_units" in h5f:
            for unit_name in h5f["/dataset_units"]:
                current_unit = h5f["/dataset_units/%s" % unit_name]
                value = current_unit.value
                unit = current_unit.attrs["unit"]
                # need to convert to a Unit object and check dimensions
                # because unit can be things like
                # 'dimensionless/dimensionless**3' so naive string
                # comparisons are insufficient
                unit = Unit(unit, registry=self.unit_registry)
                if unit_name.endswith('_unit') and unit.dimensions is sympy_one:
                    # Catch code units and if they are dimensionless,
                    # assign CGS units. setdefaultattr will catch code units
                    # which have already been set via units_override. 
                    un = unit_name[:-5]
                    un = un.replace('magnetic', 'magnetic_field', 1)
                    unit = unit_system_registry["cgs"][un]
                setdefaultattr(self, unit_name, self.quan(value, unit))
                if unit_name in h5f["/field_types"]:
                    if unit_name in self.field_units:
                        mylog.warning("'field_units' was overridden by 'dataset_units/%s'"
                                      % (unit_name))
                    if value == 1.0:
                        self.field_units[unit_name] = str(unit)
                    else:
                        self.field_units[unit_name] = "{} * {}".format(value, str(unit))
        else:
            setdefaultattr(self, 'length_unit', self.quan(1.0, "cm"))
            setdefaultattr(self, 'mass_unit', self.quan(1.0, "g"))
            setdefaultattr(self, 'time_unit', self.quan(1.0, "s"))

        h5f.close()

    def _parse_parameter_file(self):
        self._handle = h5py.File(self.parameter_filename, "r")
        if 'data_software' in self._handle['gridded_data_format'].attrs:
            self.data_software = \
                self._handle['gridded_data_format'].attrs['data_software']
        else:
            self.data_software = "unknown"
        sp = self._handle["/simulation_parameters"].attrs
        if self.geometry is None:
            geometry = just_one(sp.get("geometry", 0))
            try:
                self.geometry = GEOMETRY_TRANS[geometry]
            except KeyError:
                raise YTGDFUnknownGeometry(geometry)
        self.parameters.update(sp)
        self.domain_left_edge = sp["domain_left_edge"][:]
        self.domain_right_edge = sp["domain_right_edge"][:]
        self.domain_dimensions = sp["domain_dimensions"][:]
        refine_by = sp["refine_by"]
        if refine_by is None:
            refine_by = 2
        self.refine_by = refine_by
        self.dimensionality = just_one(sp["dimensionality"])
        self.current_time = just_one(sp.get("current_time", 0))
        self.unique_identifier = sp["unique_identifier"]
        self.cosmological_simulation = just_one(sp["cosmological_simulation"])
        if sp["num_ghost_zones"] != 0:
            raise RuntimeError
        self.num_ghost_zones = just_one(sp["num_ghost_zones"])
        self.field_ordering = just_one(sp.get("field_ordering", 0))
        try:
            self.boundary_conditions = sp["boundary_conditions"][:]
        except KeyError:
            self.boundary_conditions = [0, 0, 0, 0, 0, 0]
        p = [bnd == 0 for bnd in self.boundary_conditions[::2]]
        self.periodicity = ensure_tuple(p)
        if self.cosmological_simulation:
            self.current_redshift = just_one(sp["current_redshift"])
            self.omega_lambda = just_one(sp["omega_lambda"])
            self.omega_matter = just_one(sp["omega_matter"])
            self.hubble_constant = just_one(sp["hubble_constant"])
        else:
            self.current_redshift = self.omega_lambda = self.omega_matter = \
                self.hubble_constant = self.cosmological_simulation = 0.0
        self.parameters['Time'] = 1.0  # Hardcode time conversion for now.
        # Hardcode for now until field staggering is supported.
        self.parameters["HydroMethod"] = 0
        self.particle_types = {"dark_matter"}.union({
            ptype for ptype in self._handle.get("particle_types", {}).keys()
        })
        self.particle_types = tuple(self.particle_types)
        self.particle_types_raw = self.particle_types
        self._handle.close()
        del self._handle

    @classmethod
    def _is_valid(self, *args, **kwargs):
        try:
            fileh = h5py.File(args[0], 'r')
            if "gridded_data_format" in fileh:
                fileh.close()
                return True
            fileh.close()
        except Exception: pass
        return False

    def __repr__(self):
        return self.basename.rsplit(".", 1)[0]
