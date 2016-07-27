import datetime
import logging

from django.views.generic.base import TemplateView, View
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib import messages
from django.http import HttpResponse, HttpResponseBadRequest, HttpResponseRedirect

from actionlog.mixins import LogActionMixin
from actionlog.models import ActionLogEntry
from adjallocation.models import DebateAdjudicator
from divisions.models import Division
from participants.models import Adjudicator, Team
from standings.teams import TeamStandingsGenerator
from tournaments.mixins import PublicTournamentPageMixin, RoundMixin, TournamentMixin
from tournaments.models import Round
from utils.mixins import CacheMixin, PostOnlyRedirectView, SuperuserRequiredMixin, VueTableTemplateView
from utils.misc import reverse_round
from utils.tables import TabbycatTableBuilder
from venues.allocator import allocate_venues
from venues.models import AdjudicatorVenueConstraint, VenueGroup

from .dbutils import delete_round_draw
from .generator import DrawError
from .manager import DrawManager
from .models import Debate, DebateTeam, TeamPositionAllocation
from .prefetch import populate_history

logger = logging.getLogger(__name__)


TPA_MAP = {
    TeamPositionAllocation.POSITION_AFFIRMATIVE: "Aff",
    TeamPositionAllocation.POSITION_NEGATIVE: "Neg",
    None: "—"
}


# ==============================================================================
# Viewing Draw (Public)
# ==============================================================================

class BaseDrawTableView(RoundMixin, VueTableTemplateView):

    template_name = 'draw_display_by.html'
    sort_key = 'Venue'
    popovers = True

    def get_page_title(self):
        return 'Draw for %s' % self.get_round().name

    def get_page_emoji(self):
        if self.get_round().draw_status == Round.STATUS_RELEASED:
            return '👏'
        else:
            return '😴'

    def get_page_subtitle(self):
        round = self.get_round()
        if round.starts_at:
            return 'debates start at %s' % round.starts_at.strftime('%H:%M')
        else:
            return ''

    def get_context_data(self, **kwargs):
        kwargs['round'] = self.get_round()
        return super().get_context_data(**kwargs)

    def populate_table(self, draw, table, round, tournament):
        table.add_debate_venue_columns(draw)
        table.add_team_columns([d.aff_team for d in draw], hide_institution=True, key="Aff")
        table.add_team_columns([d.neg_team for d in draw], hide_institution=True, key="Neg")
        if tournament.pref('enable_division_motions'):
            table.add_motion_column(d.get_division_motions for d in draw)

        if not tournament.pref('enable_divisions'):
            table.add_debate_adjudicators_column(draw, show_splits=False)

    def get_table(self):
        tournament = self.get_tournament()
        round = self.get_round()
        draw = round.debate_set_with_prefetches()
        table = TabbycatTableBuilder(view=self, sort_key=self.sort_key, popovers=self.popovers)
        self.populate_table(draw, table, round, tournament)
        return table


class PublicDrawForRoundView(PublicTournamentPageMixin, CacheMixin, BaseDrawTableView):

    public_page_preference = 'public_draw'

    def get_template_names(self):
        round = self.get_round()
        if round.draw_status != round.STATUS_RELEASED:
            messages.info(self.request, 'The draw for ' + round.name +
                ' has yet to be released')
            return ["base.html"]
        else:
            return super().get_template_names()

    def get_context_data(self, **kwargs):
        round = self.get_round()
        if round.draw_status != round.STATUS_RELEASED:
            kwargs["round"] = self.get_round()
            return super(BaseDrawTableView, self).get_context_data(**kwargs) # skip BaseDrawTableView
        else:
            return super().get_context_data(**kwargs)


class PublicDrawForCurrentRoundView(PublicDrawForRoundView):

    def get_round(self):
        return self.get_tournament().current_round


class PublicAllDrawsAllTournamentsView(PublicTournamentPageMixin, TemplateView):
    template_name = "public_draw_display_all.html"
    public_page_preference = 'enable_mass_draws'

    def get_context_data(self, **kwargs):
        t = self.get_tournament()
        all_rounds = list(Round.objects.filter(
            tournament=t, draw_status=Round.STATUS_RELEASED))
        for r in all_rounds:
            r.draw = r.debate_set_with_prefetches()
        kwargs['all_rounds'] = all_rounds
        return super().get_context_data(**kwargs)


class AdminDrawDisplayForRoundByVenueView(LoginRequiredMixin, BaseDrawTableView):
    popovers = True


class AdminDrawDisplayForRoundByTeamView(LoginRequiredMixin, BaseDrawTableView):
    sort_key = 'Team'
    popovers = True

    def populate_table(self, draw, table, round, tournament):
        draw = list(draw) + list(draw) # Double up the draw
        draw_slice = len(draw) // 2 # Top half gets affs; bottom negs
        table.add_team_columns(
            [d.aff_team for d in draw[:draw_slice]] + [d.neg_team for d in draw[draw_slice:]],
            hide_institution=True, key="Team")
        super().populate_table(draw, table, round, tournament)


# ==============================================================================
# Draw Creation (Admin)
# ==============================================================================

class AdminDrawView(RoundMixin, SuperuserRequiredMixin, VueTableTemplateView):
    detailed = False

    page_emoji = '👏'

    def get_page_title(self):
        rd = self.get_round()
        return 'Draw for %s' % rd.name

    def get_table(self):
        r = self.get_round()
        table = TabbycatTableBuilder(view=self)
        if r.draw_status == r.STATUS_NONE:
            return table # Return Blank

        draw = r.debate_set_with_prefetches(ordering=('room_rank',))
        populate_history(draw)
        if r.is_break_round:
            table.add_room_rank_columns(draw)
        else:
            table.add_debate_bracket_columns(draw)

        table.add_debate_venue_columns(draw)
        table.add_team_columns([d.aff_team for d in draw], key="Aff",
            hide_institution=True)
        table.add_team_columns([d.neg_team for d in draw], key="Neg",
            hide_institution=True)

        # For draw details and draw draft pages
        if (r.draw_status == r.STATUS_DRAFT or self.detailed) and r.prev:
            teams = Team.objects.filter(debateteam__debate__round=r)
            metrics = r.tournament.pref('team_standings_precedence')
            generator = TeamStandingsGenerator(metrics, ('rank', 'subrank'))
            standings = generator.generate(teams, round=r.prev)
            if not r.is_break_round:
                if "points" in standings.metric_keys:
                    table.add_team_pullup_columns(draw, standings)
                table.add_debate_ranking_columns(draw, standings)
            else:
                table.add_column("Aff Break Rank", [d.aff_team.break_rank_for_category(r.break_category) for d in draw])
                table.add_column("Neg Break Rank", [d.neg_team.break_rank_for_category(r.break_category) for d in draw])
            table.add_debate_metric_columns(draw, standings)
            table.add_affs_count([d.aff_team for d in draw], r.prev, 'aff')
            table.add_affs_count([d.neg_team for d in draw], r.prev, 'neg')
        else:
            table.add_debate_adjudicators_column(draw, show_splits=False)

        table.add_draw_conflicts(draw)
        if not r.is_break_round:
            table.highlight_rows_by_column_value(column=0) # highlight first row of a new bracket

        return table

    def get_template_names(self):
        round = self.get_round()
        self.page_emoji = '👀'
        if self.detailed:
            self.page_title = 'Draw with Details for %s' % round.name
            return ["draw_base.html"]
        if round.draw_status == round.STATUS_NONE:
            self.page_title = 'No draw for %s' % round.name
            messages.warning(self.request, 'No draw exists yet — go to the ' +
                'check-ins section for this round to generate a draw.')
            return ["base.html"]
        elif round.draw_status == round.STATUS_DRAFT:
            self.page_title = 'Draft draw for %s' % round.name
            return ["draw_status_draft.html"]
        elif round.draw_status == round.STATUS_CONFIRMED:
            self.page_title = 'Draw for %s' % round.name
            return ["draw_status_confirmed.html"]
        elif round.draw_status == round.STATUS_RELEASED:
            self.page_title = 'Released draw for %s' % round.name
            return ["draw_status_confirmed.html"]
        else:
            raise


class AdminDrawWithDetailsView(AdminDrawView):
    detailed = True

    def get_page_title(self):
        rd = self.get_round()
        return 'Draw for %s with Details' % rd.name


# ==============================================================================
# Draw Status POSTS
# ==============================================================================

class DrawStatusEdit(LogActionMixin, SuperuserRequiredMixin, RoundMixin, PostOnlyRedirectView):

    def get_redirect_url(self):
        return reverse_round('draw', self.get_round())


class CreateDrawView(DrawStatusEdit):

    action_log_type = ActionLogEntry.ACTION_TYPE_DRAW_CREATE

    def post(self, request, *args, **kwargs):
        round = self.get_round()

        if round.draw_status != round.STATUS_NONE:
            messages.error(request, "Could not create draw for {}, there was already a draw!".format(round.name))
            return super().post(request, *args, **kwargs)

        manager = DrawManager(round)
        try:
            manager.create()
        except DrawError as e:
            messages.error(request, "There was a problem creating the draw: " + str(e) + " If this issue persists, please contact the developers.")
            return HttpResponseRedirect(reverse_round('availability_index', round))

        # TODO provide a more intelligent check in sites where there are multiple tournaments
        if not AdjudicatorVenueConstraint.objects.all().exists():
            allocate_venues(round)
        else:
            messages.warning(request, "Venues were not auto-allocated because there are one or more adjudicator venue constraints. "
                "You should run venue allocations after allocating adjudicators.")

        self.log_action()
        return super().post(request, *args, **kwargs)


class ConfirmDrawCreationView(DrawStatusEdit):
    action_log_type = ActionLogEntry.ACTION_TYPE_DRAW_CONFIRM

    def post(self, request, *args, **kwargs):
        round = self.get_round()
        if round.draw_status != round.STATUS_DRAFT:
            return HttpResponseBadRequest("Draw status is not DRAFT")

        round.draw_status = round.STATUS_CONFIRMED
        round.save()
        return super().post(request, *args, **kwargs)


class DrawRegenerateView(DrawStatusEdit):
    action_log_type = ActionLogEntry.ACTION_TYPE_DRAW_REGENERATE

    def post(self, request, *args, **kwargs):
        round = self.get_round()
        delete_round_draw(round)
        return super().post(request, *args, **kwargs)


class ConfirmDrawRegenerationView(SuperuserRequiredMixin, TemplateView):
    template_name = "draw_confirm_regeneration.html"


class DrawReleaseView(DrawStatusEdit):
    action_log_type = ActionLogEntry.ACTION_TYPE_DRAW_RELEASE

    def post(self, request, *args, **kwargs):
        round = self.get_round()
        if round.draw_status != round.STATUS_CONFIRMED:
            return HttpResponseBadRequest("Draw status is not CONFIRMED")

        round.draw_status = round.STATUS_RELEASED
        round.save()
        return super().post(request, *args, **kwargs)


class DrawUnreleaseView(DrawStatusEdit):
    action_log_type = ActionLogEntry.ACTION_TYPE_DRAW_UNRELEASE

    def post(self, request, *args, **kwargs):
        round = self.get_round()
        if round.draw_status != round.STATUS_RELEASED:
            return HttpResponseBadRequest("Draw status is not RELEASED")

        round.draw_status = round.STATUS_CONFIRMED
        round.save()
        return super().post(request, *args, **kwargs)


class SetRoundStartTimeView(DrawStatusEdit):
    action_log_type = ActionLogEntry.ACTION_TYPE_ROUND_START_TIME_SET

    def post(self, request, *args, **kwargs):
        time_text = request.POST["start_time"]
        try:
            time = datetime.datetime.strptime(time_text, "%H:%M").time()
        except ValueError:
            messages.error(request, "Sorry, \"{}\" isn't a valid time. It must "
                           "be in 24-hour format, with a colon, for "
                           "example: \"13:57\".".format(time_text))
            return super().post(request, *args, **kwargs)

        round = self.get_round()
        round.starts_at = time
        round.save()
        # Need to call explicitly, since this isn't a form view
        self.log_action()

        return super().post(request, *args, **kwargs)


# ==============================================================================
# Adjudicator Scheduling
# ==============================================================================

class ScheduleDebatesView(SuperuserRequiredMixin, RoundMixin, TemplateView):
    template_name = "draw_set_debate_times.html"

    def get_context_data(self, **kwargs):
        round = self.get_round()
        kwargs['venue_groups'] = VenueGroup.objects.all()
        kwargs['divisions'] = Division.objects.filter(tournament=round.tournament).order_by('id')
        return super().get_context_data(**kwargs)


class ScheduleConfirmationsView(SuperuserRequiredMixin, RoundMixin, TemplateView):
    template_name = "confirmations_view.html"

    def get_context_data(self, **kwargs):
        adjs = Adjudicator.objects.all().order_by('name')
        for adj in adjs:
            shifts = DebateAdjudicator.objects.filter(adjudicator=adj, debate__round__tournament__active=True)
            if len(shifts) > 0:
                adj.shifts = shifts
        kwargs['adjs'] = adjs
        return super().get_context_data(**kwargs)


class ApplyDebateScheduleView(DrawStatusEdit):

    def post(self, request, *args, **kwargs):
        round = self.get_round()
        debates = Debate.objects.filter(round=round)
        for debate in debates:
            division = debate.teams[0].division
            if division and division.time_slot:
                date = request.POST[str(division.venue_group.id)]
                if date:
                    time = "%s %s" % (date, division.time_slot)
                    try:
                        debate.time = datetime.datetime.strptime(
                            time, "%Y-%m-%d %H:%M:%S")  # Chrome
                    except ValueError:
                        debate.time = datetime.datetime.strptime(
                            time, "%d/%m/%Y %H:%M:%S")  # Others

                    debate.save()

        messages.success(self.request, "Applied schedules to debates")
        return super().post(request, *args, **kwargs)


# ==============================================================================
# Sides Editing and Viewing
# ==============================================================================

class BaseSideAllocationsView(TournamentMixin, VueTableTemplateView):

    page_title = "Side Pre-Allocations"

    def get_table(self):
        tournament = self.get_tournament()
        teams = tournament.team_set.all()
        rounds = tournament.prelim_rounds()

        tpas = dict()
        for tpa in TeamPositionAllocation.objects.filter(round__in=rounds):
            tpas[(tpa.team.id, tpa.round.seq)] = TPA_MAP[tpa.position]

        table = TabbycatTableBuilder(view=self)
        table.add_team_columns(teams)

        headers = [round.abbreviation for round in rounds]
        data = [[tpas.get((team.id, round.id), "—") for round in rounds] for team in teams]
        table.add_columns(headers, data)

        return table


class SideAllocationsView(SuperuserRequiredMixin, BaseSideAllocationsView):
    pass


class PublicSideAllocationsView(PublicTournamentPageMixin, BaseSideAllocationsView):
    public_page_preference = 'public_side_allocations'


class DrawMatchupsEditView(SuperuserRequiredMixin, RoundMixin, TemplateView):
    template_name = 'draw_matchups_edit.html'

    def get_context_data(self, **kwargs):
        round = self.get_round()
        kwargs['unused_teams'] = round.unused_teams()
        kwargs['draw'] = round.debate_set_with_prefetches(ordering=('room_rank',))
        possible_debates = len(kwargs['unused_teams']) // 2 + 1
        kwargs['possible_debates'] = [None] * possible_debates
        return super().get_context_data(**kwargs)


class SaveDrawMatchups(SuperuserRequiredMixin, RoundMixin, View):

    def post(self, request, *args, **kwargs):
        existing_debate_ids = [int(a.replace('debate_', ''))
                               for a in list(request.POST.keys())
                               if a.startswith('debate_')]
        for debate_id in existing_debate_ids:
            debate = Debate.objects.get(id=debate_id)
            new_aff_id = request.POST.get('aff_%s' % debate_id).replace('team_',
                                                                        '')
            new_neg_id = request.POST.get('neg_%s' % debate_id).replace('team_',
                                                                        '')

            if new_aff_id and new_neg_id:
                DebateTeam.objects.filter(debate=debate).delete()
                debate.save()

                new_aff_team = Team.objects.get(id=int(new_aff_id))
                new_aff_dt = DebateTeam(debate=debate,
                                        team=new_aff_team,
                                        position=DebateTeam.POSITION_AFFIRMATIVE)
                new_aff_dt.save()

                new_aff_team = Team.objects.get(id=int(new_neg_id))
                new_neg_dt = DebateTeam(debate=debate,
                                        team=new_aff_team,
                                        position=DebateTeam.POSITION_NEGATIVE)
                new_neg_dt.save()
            else:
                # If there's blank debates we need to delete those
                debate.delete()

        new_debate_ids = [int(a.replace('new_debate_', ''))
                          for a in list(request.POST.keys())
                          if a.startswith('new_debate_')]
        for debate_id in new_debate_ids:
            new_aff_id = request.POST.get('aff_%s' % debate_id).replace('team_',
                                                                        '')
            new_neg_id = request.POST.get('neg_%s' % debate_id).replace('team_',
                                                                        '')

            if new_aff_id and new_neg_id:
                debate = Debate(round=round, venue=None)
                debate.save()

                aff_team = Team.objects.get(id=int(new_aff_id))
                neg_team = Team.objects.get(id=int(new_neg_id))
                new_aff_dt = DebateTeam(debate=debate,
                                        team=aff_team,
                                        position=DebateTeam.POSITION_AFFIRMATIVE)
                new_neg_dt = DebateTeam(debate=debate,
                                        team=neg_team,
                                        position=DebateTeam.POSITION_NEGATIVE)
                new_aff_dt.save()
                new_neg_dt.save()

        return HttpResponse("ok")
