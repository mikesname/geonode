#########################################################################
#
# Copyright (C) 2016 OSGeo
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

from typing import List

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from rdflib import Graph, Literal
from rdflib.namespace import RDF, SKOS, DC, DCTERMS

from geonode.base.models import Thesaurus, ThesaurusKeyword, ThesaurusKeywordLabel, ThesaurusLabel


class Command(BaseCommand):

    help = 'Load a thesaurus in RDF format into DB'

    def add_arguments(self, parser):

        # Named (optional) arguments
        parser.add_argument(
            '-d',
            '--dry-run',
            action="store_true",
            dest='dryrun',
            default=False,
            help='Only parse and print the thesaurus file, without perform insertion in the DB.')

        parser.add_argument(
            '--name',
            dest='name',
            help='Identifier name for the thesaurus in this GeoNode instance.')

        parser.add_argument(
            '--file',
            dest='file',
            help='Full path to a thesaurus in RDF format.')

    def handle(self, **options):

        input_file = options.get('file')
        name = options.get('name')
        dryrun = options.get('dryrun')

        if not input_file:
            raise CommandError("Missing thesaurus rdf file path (--file)")

        if not name:
            raise CommandError("Missing identifier name for the thesaurus (--name)")

        if name.startswith('fake'):
            self.create_fake_thesaurus(name)
        else:
            self.load_thesaurus(input_file, name, not dryrun)

    def load_thesaurus(self, input_file, name, store):
        g = Graph()
        g.parse(input_file)

        # An error will be thrown here there is more than one scheme in the file
        scheme = g.value(None, RDF.type, SKOS.ConceptScheme, any=False)
        if scheme is None:
            raise CommandError("ConceptScheme not found in file")

        default_lang = getattr(settings, 'THESAURUS_DEFAULT_LANG', None)

        available_titles = [t for t in g.objects(scheme, DC.title) if isinstance(t, Literal)]
        thesaurus_title = value_for_language(available_titles, default_lang)
        description = g.value(scheme, DC.description, None, default=thesaurus_title)
        date_issued = g.value(scheme, DCTERMS.issued, None)

        print(f'Thesaurus "{thesaurus_title}", desc: {description} issued at {date_issued}')

        thesaurus = Thesaurus()
        thesaurus.identifier = name
        thesaurus.description = description
        thesaurus.title = thesaurus_title
        thesaurus.about = str(scheme)
        thesaurus.date = date_issued

        if store:
            thesaurus.save()

        for lang in available_titles:
            if lang.language is not None:
                thesaurus_label = ThesaurusLabel()
                thesaurus_label.lang = lang[0]
                thesaurus_label.label = lang[1]
                thesaurus_label.thesaurus = thesaurus

                if store:
                    thesaurus_label.save()

        for concept in g.subjects(RDF.type, SKOS.Concept):
            pref = g.preferredLabel(concept, default_lang)[0][1]
            about = str(concept)
            alt_label = g.value(concept, SKOS.altLabel, object=None, default=None)
            if alt_label is not None:
                alt_label = str(alt_label)
            else:
                available_labels = [t for t in g.objects(concept, SKOS.prefLabel) if isinstance(t, Literal)]
                alt_label = value_for_language(available_labels, default_lang)

            print(f'Concept {str(pref)}: {alt_label} ({about})')

            tk = ThesaurusKeyword()
            tk.thesaurus = thesaurus
            tk.about = about
            tk.alt_label = alt_label

            if store:
                tk.save()

            for _, pref_label in g.preferredLabel(concept):
                lang = pref_label.language
                # remove script code from lang if it exists
                if "-" in lang:
                    lang = lang.split("-")[0]
                label = str(pref_label)
                print(f'    Label {lang}: {label}')

                tkl = ThesaurusKeywordLabel()
                tkl.keyword = tk
                tkl.lang = lang
                tkl.label = label

                if store:
                    tkl.save()


    def create_fake_thesaurus(self, name):
        thesaurus = Thesaurus()
        thesaurus.identifier = name

        thesaurus.title = f"Title: {name}"
        thesaurus.description = "SAMPLE FAKE THESAURUS USED FOR TESTING"
        thesaurus.date = "2016-10-01"

        thesaurus.save()

        for keyword in ['aaa', 'bbb', 'ccc']:
            tk = ThesaurusKeyword()
            tk.thesaurus = thesaurus
            tk.about = f"{keyword}_about"
            tk.alt_label = f"{keyword}_alt"
            tk.save()

            for _l in ['it', 'en', 'es']:
                tkl = ThesaurusKeywordLabel()
                tkl.keyword = tk
                tkl.lang = _l
                tkl.label = f"{keyword}_l_{_l}_t_{name}"
                tkl.save()


def value_for_language(available: List[Literal], default_lang: str) -> str:
    sorted_lang = sorted(available, key=lambda literal: '' if literal.language is None else literal.language)
    for item in sorted_lang:
        if item.language is None:
            return str(item)
        elif item.language.split("-")[0] == default_lang:
            return str(item)
    return str(available[0])
