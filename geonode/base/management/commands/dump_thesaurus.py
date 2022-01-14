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

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from rdflib import Graph, URIRef, Literal
from rdflib.namespace import DC, DCTERMS, RDF, SKOS

from geonode.base.models import Thesaurus, ThesaurusKeyword, ThesaurusKeywordLabel, ThesaurusLabel


class Command(BaseCommand):

    help = 'Dump a thesaurus in RDF format'

    def add_arguments(self, parser):

        # Named (optional) arguments
        parser.add_argument(
            '-n',
            '--name',
            dest='name',
            help='Dump the thesaurus with the given name')

        parser.add_argument(
            '-f',
            '--format',
            dest='format',
            default='pretty-xml',
            help='Format string supported by rdflib, e.g.: pretty-xml (default), xml, n3, ttl, json-ld'
        )

        parser.add_argument(
            '--default-lang',
            dest='lang',
            default=getattr(settings, 'THESAURUS_DEFAULT_LANG', None),
            help='Default language code for untagged string literals'
        )

        # Named (optional) arguments
        parser.add_argument(
            '-l',
            '--list',
            action="store_true",
            dest='list',
            default=False,
            help='List available thesauri')

    def handle(self, **options):

        name = options.get('name')
        list = options.get('list')

        if not name and not list:
            raise CommandError("Missing identifier name for the thesaurus (--name)")

        if list:
            self.list_thesauri()
            return

        self.dump_thesaurus(name, options.get('format'), options.get('lang'))

    def list_thesauri(self):
        print('LISTING THESAURI')
        max_id_len = len(max(Thesaurus.objects.values_list('identifier', flat=True), key=len))

        for t in Thesaurus.objects.order_by('order').all():
            if t.card_max == 0:
                card = 'DISABLED'
            else:
                # DISABLED
                # [0..n]
                card = f'[{t.card_min}..{t.card_max if t.card_max!=-1 else "N"}]  '
            print(f'id:{t.id:2} sort:{t.order:3} {card} name={t.identifier.ljust(max_id_len)} title="{t.title}" URI:{t.about}')

    def dump_thesaurus(self, name: str, format: str, default_lang: str):

        g = Graph()
        thesaurus = Thesaurus.objects.filter(identifier=name).get()
        scheme = URIRef(thesaurus.about)
        g.add((scheme, RDF.type, SKOS.ConceptScheme))
        g.add((scheme, DC.title, Literal(thesaurus.title, lang=default_lang)))
        g.add((scheme, DC.description, Literal(thesaurus.description)))
        g.add((scheme, DCTERMS.issued, Literal(thesaurus.date)))

        for title_label in ThesaurusLabel.objects.filter(thesaurus=thesaurus).all():
            g.add((scheme, DC.title, Literal(title_label.label, lang=title_label.lang)))

        # Concepts
        for keyword in ThesaurusKeyword.objects.filter(thesaurus=thesaurus).all():
            concept = URIRef(keyword.about)
            g.add((concept, RDF.type, SKOS.Concept))
            g.add((concept, SKOS.inScheme, scheme))
            if keyword.alt_label:
                g.add((concept, SKOS.altLabel, Literal(keyword.alt_label, lang=default_lang)))
            for label in ThesaurusKeywordLabel.objects.filter(keyword=keyword).all():
                g.add((concept, SKOS.prefLabel, Literal(label.label, lang = label.lang)))

        print(g.serialize(format=format))
