"""Stopword lists for the stopword-ratio language detector (design doc 02, section 6.3).

Stored as Python literals so no package data files are needed. Roughly 100
high-frequency function words per language; overlap between languages is
expected and harmless because the winner is the highest hit ratio.
"""

from __future__ import annotations

from typing import Dict, FrozenSet, Tuple

STOPWORDS: Dict[str, FrozenSet[str]] = {
    "en": frozenset(
        """
        the of and to in a is that for it as was with be by on not he this are or his
        from at which but have an had they you were their one all we can her has there
        been if more when will would who so no she what up its about into than them
        only other new some could time these two may then do first any my now such
        like our over man me even most made after also did many before must through
        where much your way well down should because each just those people how too
        own very long here between while might same off being both under never same
        without being every another
        """.split()
    ),
    "tr": frozenset(
        """
        ve bir bu da de için ile olarak daha çok en ne gibi ama kadar sonra var
        her o ben sen biz siz onlar bana sana ona bize size onlara beni seni onu
        bizi sizi onları benim senin onun bizim sizin onların şu şey şimdi
        değil ki mi mı mu mü ise ya veya hem hiç böyle şöyle öyle nasıl neden
        niçin niye çünkü fakat ancak lakin yani zaten artık henüz hâlâ yine gene
        bile hep hepsi bütün tüm bazı birkaç birçok kendi kendisi diğer başka
        aynı ilk son önce sonra üzerinde altında içinde arasında karşı doğru
        göre kadar beri dolayı rağmen ötürü ait olan olduğu oldu olur olmak
        etmek yapmak demek gelmek gitmek vermek almak bilmek görmek
        """.split()
    ),
    "de": frozenset(
        """
        der die das und in zu den nicht von sie ist des sich mit dem dass er es ein
        ich auf so eine auch als an nach wie im für man aber aus durch wenn nur war
        noch werden bei hat wir was wird sein einen welche sind oder zur um haben
        einer mir über ihm diese einem ihr uns da zum kann doch vor dieser mich ihn
        du hatte seine mehr am denn nun unter sehr selbst schon hier bis habe ihre
        dann ihnen seiner alle wieder meine zeit gegen vom ganz einzelnen wo muss
        ohne eines können sei jetzt immer mein solche ihrem viel während dieses
        dies würde weil keine sondern wurde damit hätte
        """.split()
    ),
    "fr": frozenset(
        """
        le la les de des du un une et est en que qui dans pour pas ce il elle ils
        elles je tu nous vous on ne se sa son ses leur leurs au aux par sur avec
        plus mais ou où comme si tout tous toute toutes cette cet ces mon ma mes
        ton ta tes notre votre nos vos y a été être avoir fait faire dit peut
        sont ont était étaient sera aussi bien très même encore déjà jamais
        toujours alors donc car puis entre sans sous vers chez depuis pendant
        avant après quand lorsque quel quelle quels quelles dont celui celle ceux
        celles autre autres chaque rien personne peu beaucoup ici là
        """.split()
    ),
    "es": frozenset(
        """
        el la los las de del un una unos unas y o que en es por para con sin como
        se su sus al lo le les mi mis tu tus nuestro nuestra vuestro este esta
        estos estas ese esa esos esas aquel aquella yo tú él ella nosotros
        vosotros ellos ellas me te nos os no sí más pero muy también ya ha han
        había fue era son están está estaba hay ser estar tener hacer sobre entre
        hasta desde durante contra hacia según cuando donde mientras porque
        aunque entonces así todo todos toda todas otro otra otros cada nada algo
        alguien nadie mucho poco siempre nunca ahora aquí allí bien mal
        """.split()
    ),
    "it": frozenset(
        """
        il lo la i gli le di del della dei delle un uno una e o che è in a da per
        con su tra fra non si sono era erano ha hanno ho hai abbiamo essere avere
        fare come se ma anche più molto poco ancora già mai sempre ora qui lì
        questo questa questi queste quello quella quelli quelle io tu lui lei noi
        voi loro mi ti ci vi mio mia tuo tua suo sua nostro vostro al allo alla
        ai agli alle dal dallo dalla dai nel nello nella nei negli nelle sul sullo
        sulla sui sugli sulle quando dove perché mentre però quindi allora
        dopo prima sopra sotto senza contro verso tutto tutti ogni altro altra
        """.split()
    ),
    "nl": frozenset(
        """
        de het een en van in is dat op te zijn voor met die niet aan er ook als
        maar om dan nog bij uit naar over door zo wat wel geen was hij zij ze we
        wij jij je ik u mij me jou hem haar ons hun hen dit deze die dat daar hier
        wie waar hoe waarom wanneer toen omdat want dus of tot al meer veel
        heeft hebben had hadden wordt worden werd werden kan kunnen zal zullen
        moet moeten mag mogen zou zouden alle alles iets niets iemand niemand
        altijd nooit nu weer heel erg zeer onder boven tussen tegen zonder
        """.split()
    ),
    "pt": frozenset(
        """
        o a os as de do da dos das um uma uns umas e ou que em é por para com sem
        como se seu sua seus suas ao aos à às no na nos nas lo la eu tu ele ela
        nós vós eles elas me te nos vos lhe lhes meu minha teu tua nosso nossa
        este esta estes estas esse essa esses essas aquele aquela isto isso
        aquilo não sim mais mas muito também já foi era são estão está estava
        há ser estar ter fazer sobre entre até desde durante contra quando onde
        enquanto porque embora então assim todo todos toda todas outro outra
        cada nada algo alguém ninguém pouco sempre nunca agora aqui ali bem mal
        """.split()
    ),
}

LANGUAGE_ORDER: Tuple[str, ...] = ("en", "tr", "de", "fr", "es", "it", "nl", "pt")
