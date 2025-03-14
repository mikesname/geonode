#########################################################################
#
# Copyright (C) 2021 OSGeo
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.
#
#########################################################################
import copy
import json
import typing
import logging
import importlib

from uuid import uuid1
from abc import ABCMeta, abstractmethod

from guardian.models import (
    UserObjectPermission,
    GroupObjectPermission)
from guardian.shortcuts import (
    assign_perm,
    get_anonymous_user)

from django.conf import settings
from django.db import transaction
from django.db.models.query import QuerySet
from django.contrib.auth.models import Group
from django.templatetags.static import static
from django.contrib.auth import get_user_model
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.contrib.contenttypes.models import ContentType

from geonode.thumbs.utils import MISSING_THUMB
from geonode.security.permissions import (
    PermSpecCompact,
    VIEW_PERMISSIONS,
    ADMIN_PERMISSIONS,
    DOWNLOAD_PERMISSIONS,
    DOWNLOADABLE_RESOURCES,
    DATASET_EDIT_DATA_PERMISSIONS,
    DATA_EDITABLE_RESOURCES_SUBTYPES,)
from geonode.groups.conf import settings as groups_settings
from geonode.security.utils import (
    perms_as_set,
    get_user_groups,
    set_owner_permissions,
    get_obj_group_managers,
    skip_registered_members_common_group)

from . import settings as rm_settings
from .utils import (
    update_resource,
    metadata_storers,
    resourcebase_post_save)

from ..base import enumerations
from ..services.models import Service
from ..base.models import ResourceBase
from ..layers.metadata import parse_metadata
from ..documents.models import Document, DocumentResourceLink
from ..layers.models import Dataset, Attribute
from ..maps.models import Map
from ..storage.manager import storage_manager

logger = logging.getLogger(__name__)


class ResourceManagerInterface(metaclass=ABCMeta):

    @abstractmethod
    def search(self, filter: dict, /, resource_type: typing.Optional[object]) -> QuerySet:
        """Returns a QuerySet of the filtered resources into the DB.

         - The 'filter' parameter should be an dictionary with the filtering criteria;
           - 'filter' = None won't return any result
           - 'filter' = {} will return the whole set
         - The 'resource_type' parameter allows to specify the concrete resource model (e.g. Dataset, Document, Map, ...)
           - 'resource_type' must be a class
           - 'resource_type' = Dataset will return a set of the only available Layers
        """
        pass

    @abstractmethod
    def exists(self, uuid: str, /, instance: ResourceBase = None) -> bool:
        """Returns 'True' or 'False' if the resource exists or not.

         - If 'instance' is provided, it will take precedence on 'uuid'
         - The existance criteria might be subject to the 'concrete resource manager' one, dependent on the resource type
           e.g.: a local Dataset existance check will be constrained by the existance of the layer on the GIS backend
        """
        pass

    @abstractmethod
    def delete(self, uuid: str, /, instance: ResourceBase = None) -> int:
        """Deletes a resource from the DB.

         - If 'instance' is provided, it will take precedence on 'uuid'
         - It will also fallback to the 'concrete resource manager' delete model.
         - This will eventually delete the related resources on the GIS backend too.
        """
        pass

    @abstractmethod
    def create(self, uuid: str, /, resource_type: typing.Optional[object] = None, defaults: dict = {}) -> ResourceBase:
        """The method will just create a new 'resource_type' on the DB model and invoke the 'post save' triggers.

         - It assumes any GIS backend resource (e.g. layers on GeoServer) already exist.
         - It is possible to pass initial default values, like the 'files' from the 'storage_manager' trhgouh the 'defaults' dictionary
        """
        pass

    @abstractmethod
    def update(self, uuid: str, /, instance: ResourceBase = None, xml_file: str = None, metadata_uploaded: bool = False,
               vals: dict = {}, regions: dict = {}, keywords: dict = {}, custom: dict = {}, notify: bool = True) -> ResourceBase:
        """The method will update an existing 'resource_type' on the DB model and invoke the 'post save' triggers.

         - It assumes any GIS backend resource (e.g. layers on GeoServer) already exist.
         - It is possible to pass initial default values, like the 'files' from the 'storage_manager' trhgouh the 'vals' dictionary
         - The 'xml_file' parameter allows to fetch metadata values from a file
         - The 'notify' parameter allows to notify the members that the resource has been updated
        """
        pass

    @abstractmethod
    def ingest(self, files: typing.List[str], /, uuid: str = None, resource_type: typing.Optional[object] = None, defaults: dict = {}, **kwargs) -> ResourceBase:
        """The method allows to create a resource by providing the list of files.

        e.g.:
            In [1]: from geonode.resource.manager import resource_manager

            In [2]: from geonode.layers.models import Dataset

            In [3]: from django.contrib.auth import get_user_model

            In [4]: admin = get_user_model().objects.get(username='admin')

            In [5]: files = ["/.../san_andres_y_providencia_administrative.dbf", "/.../san_andres_y_providencia_administrative.prj",
            ...:  "/.../san_andres_y_providencia_administrative.shx", "/.../san_andres_y_providencia_administrative.sld", "/.../san_andres_y_providencia_administrative.shp"]

            In [6]: resource_manager.ingest(files, resource_type=Dataset, defaults={'owner': admin})
        """
        pass

    @abstractmethod
    def copy(self, instance: ResourceBase, /, uuid: str = None, owner: settings.AUTH_USER_MODEL = None, defaults: dict = {}) -> ResourceBase:
        """The method makes a copy of the existing resource.

         - It makes a copy of the files
         - It creates a new layer on the GIS backend in the case the ResourceType is a Dataset
        """
        pass

    @abstractmethod
    def append(self, instance: ResourceBase, vals: dict = {}) -> ResourceBase:
        """The method appends data to an existing resource.

         - It assumes any GIS backend resource (e.g. layers on GeoServer) already exist.
        """
        pass

    @abstractmethod
    def replace(self, instance: ResourceBase, vals: dict = {}) -> ResourceBase:
        """The method replaces data of an existing resource.

         - It assumes any GIS backend resource (e.g. layers on GeoServer) already exist.
        """
        pass

    @abstractmethod
    def exec(self, method: str, uuid: str, /, instance: ResourceBase = None, **kwargs) -> ResourceBase:
        """A generic 'exec' method allowing to invoke specific methods of the concrete resource manager not exposed by the interface.

         - The parameter 'method' represents the actual name of the concrete method to invoke.
        """
        pass

    @abstractmethod
    def remove_permissions(self, uuid: str, /, instance: ResourceBase = None) -> bool:
        """Completely cleans the permissions of a resource, resetting it to the default state (owner only)
        """
        pass

    @abstractmethod
    def set_permissions(self, uuid: str, /, instance: ResourceBase = None, owner: settings.AUTH_USER_MODEL = None, permissions: dict = {}, created: bool = False) -> bool:
        """Sets the permissions of a resource.

         - It optionally gets a JSON 'perm_spec' through the 'permissions' parameter
         - If no 'perm_spec' is provided, it will set the default permissions (owner only)
        """
        pass

    @abstractmethod
    def get_workflow_permissions(self, uuid: str, /, instance: ResourceBase = None, permissions: dict = {}) -> dict:
        """Fix-up the permissions of a Resource accordingly to the currently active advanced workflow configuraiton"""
        pass

    @abstractmethod
    def set_thumbnail(self, uuid: str, /, instance: ResourceBase = None, overwrite: bool = True, check_bbox: bool = True) -> bool:
        """Allows to generate or re-generate the Thumbnail of a Resource."""
        pass


class ResourceManager(ResourceManagerInterface):

    def __init__(self):
        self._concrete_resource_manager = self._get_concrete_manager()

    def _get_concrete_manager(self):
        module_name, class_name = rm_settings.RESOURCE_MANAGER_CONCRETE_CLASS.rsplit(".", 1)
        module = importlib.import_module(module_name)
        class_ = getattr(module, class_name)
        return class_()

    @classmethod
    def _get_instance(cls, uuid: str) -> ResourceBase:
        _resources = ResourceBase.objects.filter(uuid=uuid)
        _exists = _resources.count() == 1
        if _exists:
            return _resources.get()
        return None

    def search(self, filter: dict, /, resource_type: typing.Optional[object]) -> QuerySet:
        _class = resource_type or ResourceBase
        _resources_queryset = _class.objects.filter(**filter)
        _filter = self._concrete_resource_manager.search(filter, resource_type=_class)
        if _filter:
            _resources_queryset.filter(_filter)
        return _resources_queryset

    def exists(self, uuid: str, /, instance: ResourceBase = None) -> bool:
        _resource = instance or ResourceManager._get_instance(uuid)
        if _resource:
            return self._concrete_resource_manager.exists(uuid, instance=_resource)
        return False

    @transaction.atomic
    def delete(self, uuid: str, /, instance: ResourceBase = None) -> int:
        _resource = instance or ResourceManager._get_instance(uuid)
        uuid = uuid or _resource.uuid
        if _resource and ResourceBase.objects.filter(uuid=uuid).exists():
            try:
                _resource.set_processing_state(enumerations.STATE_RUNNING)
                self._concrete_resource_manager.delete(uuid, instance=_resource)
                try:
                    if isinstance(_resource.get_real_instance(), Dataset):
                        """
                        - Remove any associated style to the layer, if it is not used by other layers.
                        - Default style will be deleted in post_delete_dataset.
                        - Remove the layer from any associated map, if any.
                        - Remove the layer default style.
                        """
                        try:
                            from geonode.maps.models import MapLayer
                            logger.debug(
                                "Going to delete associated maplayers for [%s]", _resource.get_real_instance().name)
                            MapLayer.objects.filter(
                                name=_resource.get_real_instance().alternate,
                                ows_url=_resource.get_real_instance().ows_url).delete()
                        except Exception as e:
                            logger.exception(e)

                        try:
                            from pinax.ratings.models import OverallRating
                            ct = ContentType.objects.get_for_model(_resource.get_real_instance())
                            OverallRating.objects.filter(
                                content_type=ct,
                                object_id=_resource.get_real_instance().id).delete()
                        except Exception as e:
                            logger.exception(e)

                        try:
                            if 'geonode.upload' in settings.INSTALLED_APPS and \
                                    settings.UPLOADER['BACKEND'] == 'geonode.importer':
                                from geonode.upload.models import Upload
                                # Need to call delete one by one in ordee to invoke the
                                #  'delete' overridden method
                                for upload in Upload.objects.filter(resource_id=_resource.get_real_instance().id):
                                    upload.delete()
                        except Exception as e:
                            logger.exception(e)

                        try:
                            _resource.get_real_instance().styles.delete()
                            _resource.get_real_instance().default_style.delete()
                        except Exception as e:
                            logger.debug(f"Error occurred while trying to delete the Dataset Styles: {e}")
                        self.remove_permissions(_resource.get_real_instance().uuid, instance=_resource.get_real_instance())
                except Exception as e:
                    logger.exception(e)

                try:
                    if _resource.remote_typename and Service.objects.filter(name=_resource.remote_typename).exists():
                        _service = Service.objects.filter(name=_resource.remote_typename).get()
                        if _service.harvester:
                            _service.harvester.harvestable_resources.filter(
                                geonode_resource__uuid=_resource.get_real_instance().uuid).update(should_be_harvested=False)
                except Exception as e:
                    logger.exception(e)
                try:
                    _resource.get_real_instance().delete()
                except ResourceBase.DoesNotExist:
                    pass
                return 1
            except Exception as e:
                logger.exception(e)
            finally:
                ResourceBase.objects.filter(uuid=uuid).delete()
        return 0

    def create(self, uuid: str, /, resource_type: typing.Optional[object] = None, defaults: dict = {}) -> ResourceBase:
        if resource_type.objects.filter(uuid=uuid).exists():
            return resource_type.objects.filter(uuid=uuid).get()
        uuid = uuid or str(uuid1())
        _resource, _created = resource_type.objects.get_or_create(
            uuid=uuid,
            defaults=defaults)
        if _resource and _created:
            _resource.set_processing_state(enumerations.STATE_RUNNING)
            try:
                with transaction.atomic():
                    _resource.set_missing_info()
                    _resource = self._concrete_resource_manager.create(uuid, resource_type=resource_type, defaults=defaults)
                _resource.set_processing_state(enumerations.STATE_PROCESSED)
            except Exception as e:
                logger.exception(e)
                self.delete(_resource.uuid, instance=_resource)
                raise e
            resourcebase_post_save(_resource.get_real_instance())
        return _resource

    def update(self, uuid: str, /, instance: ResourceBase = None, xml_file: str = None, metadata_uploaded: bool = False,
               vals: dict = {}, regions: list = [], keywords: list = [], custom: dict = {}, notify: bool = True) -> ResourceBase:
        _resource = instance or ResourceManager._get_instance(uuid)
        if _resource:
            _resource.set_processing_state(enumerations.STATE_RUNNING)
            _resource.set_missing_info()
            _resource.metadata_uploaded = metadata_uploaded
            logger.debug(f'Look for xml and finalize Dataset metadata {_resource}')
            try:
                with transaction.atomic():
                    if metadata_uploaded and xml_file:
                        _md_file = None
                        try:
                            _md_file = storage_manager.open(xml_file)
                        except Exception as e:
                            logger.exception(e)
                            _md_file = open(xml_file)

                        _resource.metadata_xml = _md_file.read()

                        _uuid, vals, regions, keywords, custom = parse_metadata(_md_file.read())
                        if uuid and uuid != _uuid:
                            raise ValidationError("The UUID identifier from the XML Metadata is different from the {_resource} one.")
                        else:
                            uuid = _uuid

                    logger.debug(f'Update Dataset with information coming from XML File if available {_resource}')
                    _resource.save()
                    _resource = update_resource(instance=_resource.get_real_instance(), regions=regions, keywords=keywords, vals=vals)
                    _resource = self._concrete_resource_manager.update(uuid, instance=_resource, notify=notify)
                    _resource = metadata_storers(_resource.get_real_instance(), custom)

                    # The following is only a demo proof of concept for a pluggable WF subsystem
                    from geonode.resource.processing.models import ProcessingWorkflow
                    _p = ProcessingWorkflow.objects.first()
                    if _p and _p.is_enabled:
                        for _task in _p.get_tasks():
                            _task.execute(_resource)
                _resource.set_processing_state(enumerations.STATE_PROCESSED)
                _resource.save(notify=notify)
            except Exception as e:
                logger.exception(e)
                _resource.set_processing_state(enumerations.STATE_INVALID)
                _resource.set_dirty_state()
            resourcebase_post_save(_resource.get_real_instance())
        return _resource

    def ingest(self, files: typing.List[str], /, uuid: str = None, resource_type: typing.Optional[object] = None, defaults: dict = {}, **kwargs) -> ResourceBase:
        instance = None
        to_update = defaults.copy()
        if 'files' in to_update:
            to_update.pop('files')
        try:
            with transaction.atomic():
                if resource_type == Document:
                    if files:
                        to_update['files'] = storage_manager.copy_files_list(files)
                    instance = self.create(
                        uuid,
                        resource_type=Document,
                        defaults=to_update
                    )
                elif resource_type == Dataset:
                    if files:
                        instance = self.create(
                            uuid,
                            resource_type=Dataset,
                            defaults=to_update)
                if instance:
                    instance = self._concrete_resource_manager.ingest(
                        storage_manager.copy_files_list(files),
                        uuid=instance.uuid,
                        resource_type=resource_type,
                        defaults=to_update,
                        **kwargs)
                    instance.set_processing_state(enumerations.STATE_PROCESSED)
                    instance.save(notify=False)
        except Exception as e:
            logger.exception(e)
            if instance:
                instance.set_processing_state(enumerations.STATE_INVALID)
                instance.set_dirty_state()
        if instance:
            resourcebase_post_save(instance.get_real_instance())
            # Finalize Upload
            if 'user' in to_update:
                to_update.pop('user')
            instance = self.update(instance.uuid, instance=instance, vals=to_update)
            self.set_thumbnail(instance.uuid, instance=instance)
        return instance

    def copy(self, instance: ResourceBase, /, uuid: str = None, owner: settings.AUTH_USER_MODEL = None, defaults: dict = {}) -> ResourceBase:
        if instance:
            try:
                _resource = None
                instance.set_processing_state(enumerations.STATE_RUNNING)
                _owner = owner or instance.get_real_instance().owner
                _perms = copy.copy(instance.get_real_instance().get_all_level_info())
                _resource = copy.copy(instance.get_real_instance())
                _resource.pk = _resource.id = None
                _resource.uuid = uuid or str(uuid1())
                _resource.save()
                if isinstance(instance.get_real_instance(), Document):
                    for resource_link in DocumentResourceLink.objects.filter(document=instance.get_real_instance()):
                        _resource_link = copy.copy(resource_link)
                        _resource_link.pk = _resource_link.id = None
                        _resource_link.document = _resource.get_real_instance()
                        _resource_link.save()
                if isinstance(instance.get_real_instance(), Dataset):
                    for attribute in Attribute.objects.filter(dataset=instance.get_real_instance()):
                        _attribute = copy.copy(attribute)
                        _attribute.pk = _attribute.id = None
                        _attribute.dataset = _resource.get_real_instance()
                        _attribute.save()
                if isinstance(instance.get_real_instance(), Map):
                    for maplayer in instance.get_real_instance().maplayers.iterator():
                        _maplayer = copy.copy(maplayer)
                        _maplayer.pk = _maplayer.id = None
                        _maplayer.map = _resource.get_real_instance()
                        _maplayer.save()
                to_update = storage_manager.copy(_resource).copy()
                _resource = self._concrete_resource_manager.copy(instance, uuid=_resource.uuid, defaults=to_update)
            except Exception as e:
                logger.exception(e)
                _resource = None
            finally:
                instance.set_processing_state(enumerations.STATE_PROCESSED)
                instance.save(notify=False)
            if _resource:
                _resource.set_processing_state(enumerations.STATE_PROCESSED)
                _resource.save(notify=False)
                to_update.update(defaults)
                if 'user' in to_update:
                    to_update.pop('user')
                # We need to remove any public access to the cloned dataset here
                if 'users' in _perms and ("AnonymousUser" in _perms['users'] or get_anonymous_user() in _perms['users']):
                    anonymous_user = "AnonymousUser" if "AnonymousUser" in _perms['users'] else get_anonymous_user()
                    _perms['users'].pop(anonymous_user)
                if 'groups' in _perms and ("anonymous" in _perms['groups'] or Group.objects.get(name='anonymous') in _perms['groups']):
                    anonymous_group = 'anonymous' if 'anonymous' in _perms['groups'] else Group.objects.get(name='anonymous')
                    _perms['groups'].pop(anonymous_group)
                self.set_permissions(_resource.uuid, instance=_resource, owner=_owner, permissions=_perms)
                return self.update(_resource.uuid, _resource, vals=to_update)
        return instance

    def append(self, instance: ResourceBase, vals: dict = {}):
        if self._validate_resource(instance.get_real_instance(), 'append'):
            self._concrete_resource_manager.append(instance.get_real_instance(), vals=vals)
            to_update = vals.copy()
            if instance:
                if 'user' in to_update:
                    to_update.pop('user')
                return self.update(instance.uuid, instance.get_real_instance(), vals=to_update)
        return instance

    def replace(self, instance: ResourceBase, vals: dict = {}):
        if self._validate_resource(instance.get_real_instance(), 'replace'):
            if vals.get('files', None):
                vals.update(storage_manager.replace(instance.get_real_instance(), vals.get('files')))
            self._concrete_resource_manager.replace(instance.get_real_instance(), vals=vals)
            to_update = vals.copy()
            if instance:
                if 'user' in to_update:
                    to_update.pop('user')
                return self.update(instance.uuid, instance.get_real_instance(), vals=to_update)
        return instance

    def _validate_resource(self, instance: ResourceBase, action_type: str) -> bool:
        if not isinstance(instance, Dataset) and action_type == 'append':
            raise Exception("Append data is available only for Layers")

        if isinstance(instance, Document) and action_type == "replace":
            return True

        exists = self._concrete_resource_manager.exists(instance.uuid, instance)

        if exists and action_type == "append":
            if isinstance(instance, Dataset):
                if instance.is_vector():
                    is_valid = True
        elif exists and action_type == "replace":
            is_valid = True
        else:
            raise ObjectDoesNotExist("Resource does not exists")
        return is_valid

    @transaction.atomic
    def exec(self, method: str, uuid: str, /, instance: ResourceBase = None, **kwargs) -> ResourceBase:
        _resource = instance or ResourceManager._get_instance(uuid)
        if _resource:
            if hasattr(self._concrete_resource_manager, method):
                _method = getattr(self._concrete_resource_manager, method)
                return _method(method, uuid, instance=_resource, **kwargs)
        return instance

    def remove_permissions(self, uuid: str, /, instance: ResourceBase = None) -> bool:
        """Remove object permissions on given resource.
        If is a layer removes the layer specific permissions then the
        resourcebase permissions.
        """
        _resource = instance or ResourceManager._get_instance(uuid)
        if _resource:
            _resource.set_processing_state(enumerations.STATE_RUNNING)
            try:
                with transaction.atomic():
                    logger.debug(f'Removing all permissions on {_resource}')
                    from geonode.layers.models import Dataset
                    _dataset = _resource.get_real_instance() if isinstance(_resource.get_real_instance(), Dataset) else None
                    if not _dataset:
                        try:
                            _dataset = _resource.dataset if hasattr(_resource, "layer") else None
                        except Exception:
                            _dataset = None
                    if _dataset:
                        UserObjectPermission.objects.filter(
                            content_type=ContentType.objects.get_for_model(_dataset),
                            object_pk=_resource.id
                        ).delete()
                        GroupObjectPermission.objects.filter(
                            content_type=ContentType.objects.get_for_model(_dataset),
                            object_pk=_resource.id
                        ).delete()
                    UserObjectPermission.objects.filter(
                        content_type=ContentType.objects.get_for_model(_resource.get_self_resource()),
                        object_pk=_resource.id).delete()
                    GroupObjectPermission.objects.filter(
                        content_type=ContentType.objects.get_for_model(_resource.get_self_resource()),
                        object_pk=_resource.id).delete()
                    if not self._concrete_resource_manager.remove_permissions(uuid, instance=_resource):
                        raise Exception("Could not complete concrete manager operation successfully!")
                _resource.set_processing_state(enumerations.STATE_PROCESSED)
                return True
            except Exception as e:
                logger.exception(e)
                _resource.set_processing_state(enumerations.STATE_INVALID)
                _resource.set_dirty_state()
        return False

    def set_permissions(self, uuid: str, /, instance: ResourceBase = None, owner: settings.AUTH_USER_MODEL = None, permissions: dict = {}, created: bool = False) -> bool:
        _resource = instance or ResourceManager._get_instance(uuid)
        if _resource:
            _resource = _resource.get_real_instance()
            _resource.set_processing_state(enumerations.STATE_RUNNING)
            logger.debug(f'Finalizing (permissions and notifications) on resource {instance}')
            try:
                with transaction.atomic():
                    logger.debug(f'Setting permissions {permissions} on {_resource}')

                    def assignable_perm_condition(perm, resource_type):
                        _assignable_perm_policy_condition = (perm in DOWNLOAD_PERMISSIONS and resource_type in DOWNLOADABLE_RESOURCES) or \
                            (perm in DATASET_EDIT_DATA_PERMISSIONS and resource_type in DATA_EDITABLE_RESOURCES_SUBTYPES) or \
                            (perm not in (DOWNLOAD_PERMISSIONS + DATASET_EDIT_DATA_PERMISSIONS))
                        logger.debug(f" perm: {perm} - resource_type: {resource_type} --> assignable: {_assignable_perm_policy_condition}")
                        return _assignable_perm_policy_condition

                    # default permissions for owner
                    if owner and owner != _resource.owner:
                        _resource.owner = owner
                        ResourceBase.objects.filter(uuid=_resource.uuid).update(owner=owner)
                    _owner = _resource.owner
                    _resource_type = _resource.resource_type or _resource.polymorphic_ctype.name

                    """
                    Remove all the permissions except for the owner and assign the
                    view permission to the anonymous group
                    """
                    self.remove_permissions(uuid, instance=_resource)

                    if permissions is not None and len(permissions):
                        """
                        Sets an object's the permission levels based on the perm_spec JSON.

                        the mapping looks like:
                        {
                            'users': {
                                'AnonymousUser': ['view'],
                                <username>: ['perm1','perm2','perm3'],
                                <username2>: ['perm1','perm2','perm3']
                                ...
                            },
                            'groups': [
                                <groupname>: ['perm1','perm2','perm3'],
                                <groupname2>: ['perm1','perm2','perm3'],
                                ...
                            ]
                        }
                        """
                        if PermSpecCompact.validate(permissions):
                            _permissions = PermSpecCompact(copy.deepcopy(permissions), _resource).extended
                        else:
                            _permissions = copy.deepcopy(permissions)

                        # default permissions for resource owner
                        _perm_spec = set_owner_permissions(_resource, members=get_obj_group_managers(_owner))

                        # Anonymous User group
                        if 'users' in _permissions and ("AnonymousUser" in _permissions['users'] or get_anonymous_user() in _permissions['users']):
                            anonymous_user = "AnonymousUser" if "AnonymousUser" in _permissions['users'] else get_anonymous_user()
                            anonymous_group = Group.objects.get(name='anonymous')
                            for perm in _permissions['users'][anonymous_user]:
                                if _resource_type == 'dataset' and perm in (
                                        'change_dataset_data', 'change_dataset_style',
                                        'add_dataset', 'change_dataset', 'delete_dataset'):
                                    assign_perm(perm, anonymous_group, _resource.dataset)
                                    _prev_perm = _perm_spec["groups"].get(anonymous_group, []) if "groups" in _perm_spec else []
                                    _perm_spec["groups"][anonymous_group] = set.union(perms_as_set(_prev_perm), perms_as_set(perm))
                                elif assignable_perm_condition(perm, _resource_type):
                                    assign_perm(perm, anonymous_group, _resource.get_self_resource())
                                    _prev_perm = _perm_spec["groups"].get(anonymous_group, []) if "groups" in _perm_spec else []
                                    _perm_spec["groups"][anonymous_group] = set.union(perms_as_set(_prev_perm), perms_as_set(perm))

                        # All the other users
                        if 'users' in _permissions and len(_permissions['users']) > 0:
                            for user, perms in _permissions['users'].items():
                                _user = get_user_model().objects.get(username=user)
                                if _user != _resource.owner and user != "AnonymousUser" and user != get_anonymous_user():
                                    for perm in perms:
                                        if _resource_type == 'dataset' and perm in (
                                                'change_dataset_data', 'change_dataset_style',
                                                'add_dataset', 'change_dataset', 'delete_dataset'):
                                            assign_perm(perm, _user, _resource.dataset)
                                            _prev_perm = _perm_spec["users"].get(_user, []) if "users" in _perm_spec else []
                                            _perm_spec["users"][_user] = set.union(perms_as_set(_prev_perm), perms_as_set(perm))
                                        elif assignable_perm_condition(perm, _resource_type):
                                            assign_perm(perm, _user, _resource.get_self_resource())
                                            _prev_perm = _perm_spec["users"].get(_user, []) if "users" in _perm_spec else []
                                            _perm_spec["users"][_user] = set.union(perms_as_set(_prev_perm), perms_as_set(perm))

                        # All the other groups
                        if 'groups' in _permissions and len(_permissions['groups']) > 0:
                            for group, perms in _permissions['groups'].items():
                                _group = Group.objects.get(name=group)
                                for perm in perms:
                                    if _resource_type == 'dataset' and perm in (
                                            'change_dataset_data', 'change_dataset_style',
                                            'add_dataset', 'change_dataset', 'delete_dataset'):
                                        assign_perm(perm, _group, _resource.dataset)
                                        _prev_perm = _perm_spec["groups"].get(_group, []) if "groups" in _perm_spec else []
                                        _perm_spec["groups"][_group] = set.union(perms_as_set(_prev_perm), perms_as_set(perm))
                                    elif assignable_perm_condition(perm, _resource_type):
                                        assign_perm(perm, _group, _resource.get_self_resource())
                                        _prev_perm = _perm_spec["groups"].get(_group, []) if "groups" in _perm_spec else []
                                        _perm_spec["groups"][_group] = set.union(perms_as_set(_prev_perm), perms_as_set(perm))

                        # AnonymousUser
                        if 'users' in _permissions and len(_permissions['users']) > 0:
                            if "AnonymousUser" in _permissions['users'] or get_anonymous_user() in _permissions['users']:
                                _user = get_anonymous_user()
                                anonymous_user = "AnonymousUser" if "AnonymousUser" in _permissions['users'] else get_anonymous_user()
                                perms = _permissions['users'][anonymous_user]
                                for perm in perms:
                                    if _resource_type == 'dataset' and perm in (
                                            'change_dataset_data', 'change_dataset_style',
                                            'add_dataset', 'change_dataset', 'delete_dataset'):
                                        assign_perm(perm, _user, _resource.dataset)
                                        _prev_perm = _perm_spec["users"].get(_user, []) if "users" in _perm_spec else []
                                        _perm_spec["users"][_user] = set.union(perms_as_set(_prev_perm), perms_as_set(perm))
                                    elif assignable_perm_condition(perm, _resource_type):
                                        assign_perm(perm, _user, _resource.get_self_resource())
                                        _prev_perm = _perm_spec["users"].get(_user, []) if "users" in _perm_spec else []
                                        _perm_spec["users"][_user] = set.union(perms_as_set(_prev_perm), perms_as_set(perm))
                    else:
                        # default permissions for anonymous users
                        anonymous_group, created = Group.objects.get_or_create(name='anonymous')

                        if not anonymous_group:
                            raise Exception("Could not acquire 'anonymous' Group.")

                        # default permissions for resource owner
                        _perm_spec = set_owner_permissions(_resource, members=get_obj_group_managers(_owner))

                        # Anonymous
                        anonymous_can_view = settings.DEFAULT_ANONYMOUS_VIEW_PERMISSION
                        if anonymous_can_view:
                            assign_perm('view_resourcebase',
                                        anonymous_group, _resource.get_self_resource())
                            _prev_perm = _perm_spec["groups"].get(anonymous_group, []) if "groups" in _perm_spec else []
                            _perm_spec["groups"][anonymous_group] = set.union(perms_as_set(_prev_perm), perms_as_set('view_resourcebase'))
                        else:
                            for user_group in get_user_groups(_owner):
                                if not skip_registered_members_common_group(user_group):
                                    assign_perm('view_resourcebase',
                                                user_group, _resource.get_self_resource())
                                    _prev_perm = _perm_spec["groups"].get(user_group, []) if "groups" in _perm_spec else []
                                    _perm_spec["groups"][user_group] = set.union(perms_as_set(_prev_perm), perms_as_set('view_resourcebase'))

                        if assignable_perm_condition('download_resourcebase', _resource_type):
                            anonymous_can_download = settings.DEFAULT_ANONYMOUS_DOWNLOAD_PERMISSION
                            if anonymous_can_download:
                                assign_perm('download_resourcebase',
                                            anonymous_group, _resource.get_self_resource())
                                _prev_perm = _perm_spec["groups"].get(anonymous_group, []) if "groups" in _perm_spec else []
                                _perm_spec["groups"][anonymous_group] = set.union(perms_as_set(_prev_perm), perms_as_set('download_resourcebase'))
                            else:
                                for user_group in get_user_groups(_owner):
                                    if not skip_registered_members_common_group(user_group):
                                        assign_perm('download_resourcebase',
                                                    user_group, _resource.get_self_resource())
                                        _prev_perm = _perm_spec["groups"].get(user_group, []) if "groups" in _perm_spec else []
                                        _perm_spec["groups"][user_group] = set.union(perms_as_set(_prev_perm), perms_as_set('download_resourcebase'))

                        if _resource.__class__.__name__ == 'Dataset':
                            # only for layer owner
                            assign_perm('change_dataset_data', _owner, _resource)
                            assign_perm('change_dataset_style', _owner, _resource)
                            _prev_perm = _perm_spec["users"].get(_owner, []) if "users" in _perm_spec else []
                            _perm_spec["users"][_owner] = set.union(perms_as_set(_prev_perm), perms_as_set(['change_dataset_data', 'change_dataset_style']))

                        _resource.handle_moderated_uploads()

                    # Fixup GIS Backend Security Rules Accordingly
                    if not self._concrete_resource_manager.set_permissions(
                            uuid, instance=_resource, owner=owner, permissions=_perm_spec, created=created):
                        # This might not be a severe error. E.g. for datasets outside of local GeoServer
                        logger.error(Exception("Could not complete concrete manager operation successfully!"))
                _resource.set_processing_state(enumerations.STATE_PROCESSED)
                return True
            except Exception as e:
                logger.exception(e)
                _resource.set_processing_state(enumerations.STATE_INVALID)
                _resource.set_dirty_state()
        return False

    def get_workflow_permissions(self, uuid: str, /, instance: ResourceBase = None, permissions: dict = {}) -> dict:
        """
        Adapts the provided "perm_spec" accordingly to the following schema:

                          |  N/PUBLISHED   | PUBLISHED
          --------------------------------------------
            N/APPROVED    |     GM/OWR     |     -
            APPROVED      |   registerd    |    all
          --------------------------------------------

        It also adds Group Managers as "editors" to the "perm_spec" in the case:
         - The Advanced Workflow has been enabled
         - The Group Managers are missing from the provided "perm_spec"

        Advanced Workflow Settings:

            **Scenario 1**: Default values: **AUTO PUBLISH**
            - `RESOURCE_PUBLISHING = False`
              `ADMIN_MODERATE_UPLOADS = False`

            - When user creates a resource
            - OWNER gets all the owner permissions (publish resource included)
            - ANONYMOUS can view and download

            **Scenario 2**: **SIMPLE PUBLISHING**
            - `RESOURCE_PUBLISHING = True` (Autopublishing is disabled)
              `ADMIN_MODERATE_UPLOADS = False`

            - When user creates a resource
            - OWNER gets all the owner permissions (`publish_resource` and `change_resourcebase_permissions` INCLUDED)
            - Group MANAGERS of the user's groups will get the owner permissions (`publish_resource` EXCLUDED)
            - Group MEMBERS of the user's groups will get the `view_resourcebase` permission
            - ANONYMOUS can not view and download if the resource is not published

            - When resource has a group assigned:
            - OWNER gets all the owner permissions (`publish_resource` and `change_resourcebase_permissions` INCLUDED)
            - Group MANAGERS of the *resource's group* will get the owner permissions (`publish_resource` EXCLUDED)
            - Group MEMBERS of the *resource's group* will get the `view_resourcebase` permission

            **Scenario 3**: **ADVANCED WORKFLOW**
            - `RESOURCE_PUBLISHING = True`
              `ADMIN_MODERATE_UPLOADS = True`

            - When user creates a resource
            - OWNER gets all the owner permissions (`publish_resource` and `change_resourcebase_permissions` EXCLUDED)
            - Group MANAGERS of the user's groups will get the owner permissions (`publish_resource` INCLUDED)
            - Group MEMBERS of the user's groups will get the `view_resourcebase` permission
            - ANONYMOUS can not view and download if the resource is not published

            - When resource has a group assigned:
            - OWNER gets all the owner permissions (`publish_resource` and `change_resourcebase_permissions` EXCLUDED)
            - Group MANAGERS of the resource's group will get the owner permissions (`publish_resource` INCLUDED)
            - Group MEMBERS of the resource's group will get the `view_resourcebase` permission

            **Scenario 4**: **SIMPLE WORKFLOW**
            - `RESOURCE_PUBLISHING = False`
              `ADMIN_MODERATE_UPLOADS = True`

            - **NOTE**: Is it even possibile? when the resource is automatically published, can it be un-published?
            If this combination is not allowed, we should either stop the process when reading the settings or log a warning and force a safe combination.

            - When user creates a resource
            - OWNER gets all the owner permissions (`publish_resource` and `change_resourcebase_permissions` INCLUDED)
            - Group MANAGERS of the user's groups will get the owner permissions (`publish_resource` INCLUDED)
            - Group MEMBERS of the user's group will get the `view_resourcebase` permission
            - ANONYMOUS can view and download

            Recap:
            - OWNER can always publish, except in the ADVANCED WORKFLOW
            - Group MANAGERS have publish privs only when `ADMIN_MODERATE_UPLOADS` is True (no DATA EDIT perms assigned by default)
            - Group MEMBERS have always access to the resource, except for the AUTOPUBLISH, where everybody has access to it.
        """
        _resource = instance or ResourceManager._get_instance(uuid)

        _permissions = None
        if permissions:
            if PermSpecCompact.validate(permissions):
                _permissions = PermSpecCompact(copy.deepcopy(permissions), _resource).extended
            else:
                _permissions = copy.deepcopy(permissions)

        if _resource:
            perm_spec = _permissions or copy.deepcopy(_resource.get_all_level_info())

            # Sanity checks
            if isinstance(perm_spec, str):
                perm_spec = json.loads(perm_spec)

            if "users" not in perm_spec:
                perm_spec["users"] = {}
            elif isinstance(perm_spec["users"], list):
                _users = {}
                for _item in perm_spec["users"]:
                    _users[_item[0]] = _item[1]
                perm_spec["users"] = _users

            if "groups" not in perm_spec:
                perm_spec["groups"] = {}
            elif isinstance(perm_spec["groups"], list):
                _groups = {}
                for _item in perm_spec["groups"]:
                    _groups[_item[0]] = _item[1]
                perm_spec["groups"] = _groups

            # Make sure we're dealing with "Profile"s and "Group"s...
            perm_spec = _resource.fixup_perms(perm_spec)
            _resource_type = _resource.resource_type or _resource.polymorphic_ctype.name

            if settings.ADMIN_MODERATE_UPLOADS or settings.RESOURCE_PUBLISHING:
                if _resource_type not in DOWNLOADABLE_RESOURCES:
                    view_perms = VIEW_PERMISSIONS
                else:
                    view_perms = VIEW_PERMISSIONS + DOWNLOAD_PERMISSIONS
                anonymous_group = Group.objects.get(name='anonymous')
                registered_members_group_name = groups_settings.REGISTERED_MEMBERS_GROUP_NAME
                user_groups = Group.objects.filter(
                    name__in=_resource.owner.groupmember_set.all().values_list("group__slug", flat=True))
                member_group_perm, group_managers = _resource.get_group_managers(user_groups)

                if group_managers:
                    for group_manager in group_managers:
                        prev_perms = perm_spec['users'].get(group_manager, []) if isinstance(perm_spec['users'], dict) else []
                        # AF: Should be a manager being able to change the dataset data and style too by default?
                        #     For the time being let's give to the manager "management" perms only.
                        # if _resource.polymorphic_ctype.name == 'layer':
                        #     perm_spec['users'][group_manager] = list(
                        #         set(prev_perms + view_perms + ADMIN_PERMISSIONS + LAYER_ADMIN_PERMISSIONS))
                        # else:
                        perm_spec['users'][group_manager] = list(
                            set(prev_perms + view_perms + ADMIN_PERMISSIONS))

                if member_group_perm:
                    for gr, perm in member_group_perm['groups'].items():
                        if gr != anonymous_group and gr.name != registered_members_group_name:
                            prev_perms = perm_spec['groups'].get(gr, []) if isinstance(perm_spec['groups'], dict) else []
                            perm_spec['groups'][gr] = list(set(prev_perms + perm))

                if _resource.is_approved:
                    if getattr(groups_settings, 'AUTO_ASSIGN_REGISTERED_MEMBERS_TO_REGISTERED_MEMBERS_GROUP_NAME', False):
                        registered_members_group = Group.objects.get(name=registered_members_group_name)
                        prev_perms = perm_spec['groups'].get(registered_members_group, []) if isinstance(perm_spec['groups'], dict) else []
                        perm_spec['groups'][registered_members_group] = list(set(prev_perms + view_perms))
                    else:
                        prev_perms = perm_spec['groups'].get(anonymous_group, []) if isinstance(perm_spec['groups'], dict) else []
                        perm_spec['groups'][anonymous_group] = list(set(prev_perms + view_perms))

                if _resource.is_published:
                    prev_perms = perm_spec['groups'].get(anonymous_group, []) if isinstance(perm_spec['groups'], dict) else []
                    perm_spec['groups'][anonymous_group] = list(set(prev_perms + view_perms))

            return self._concrete_resource_manager.get_workflow_permissions(_resource.uuid, instance=_resource, permissions=perm_spec)

        return _permissions

    def set_thumbnail(self, uuid: str, /, instance: ResourceBase = None, overwrite: bool = True, check_bbox: bool = True) -> bool:
        _resource = instance or ResourceManager._get_instance(uuid)
        if _resource:
            _resource.set_processing_state(enumerations.STATE_RUNNING)
            try:
                with transaction.atomic():
                    if instance and instance.files and isinstance(instance.get_real_instance(), Document):
                        if overwrite or instance.thumbnail_url == static(MISSING_THUMB):
                            from geonode.documents.tasks import create_document_thumbnail
                            create_document_thumbnail.apply((instance.id,))
                    self._concrete_resource_manager.set_thumbnail(uuid, instance=_resource, overwrite=overwrite, check_bbox=check_bbox)
                _resource.set_processing_state(enumerations.STATE_PROCESSED)
                return True
            except Exception as e:
                logger.exception(e)
        return False


resource_manager = ResourceManager()
