"""Money Idea of the Day: a fun financial-literacy curriculum for families.

Every daily recap teaches one idea with a story and analogy for each age
group the parent chose, so over a few weeks a child builds a foundation,
basics first. Each user works through the curriculum in order (skipping
ahead to an idea that fits the day, e.g. "everything moves together" on a
whole-market day, once the first few basics are done), and every idea sent
is recorded in `money_idea_sends`, so parents can look back and re-read
them with their kids.

The text is ours, not AI-written: it's free, the same for everyone, and
reviewed once. It's checked by the same language rules as AI stories (see
tests). No advice: ideas describe how things work, never what to do.

Style: every version reads like a mini-story, positive and joyful, so kids
look forward to the next one. Price dips are "dips" or "rests", not
"crashes" or "losses". The mood is playful and curious throughout.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from html import escape

from sqlalchemy import func, select
from sqlalchemy.orm import Session as DBSession

from app.models import MoneyIdeaSend

# Ideas to send in order before skipping ahead to one that fits the day.
FOUNDATION = 4

# Tags describing the day, used to pick a fitting idea.
SMALL_DAY = "small_day"     # the biggest mover only wiggled
MARKET_DAY = "market_day"   # it moved with the whole market
BIG_MOVE = "big_move"       # it moved a lot


@dataclass(frozen=True)
class MoneyIdea:
    key: str
    title: str
    # age group -> (idea, question to ask together)
    versions: dict[str, tuple[str, str]]
    tag: str | None = None


CURRICULUM: list[MoneyIdea] = [
    # ------------------------------------------------------------------
    # 1. company
    # ------------------------------------------------------------------
    MoneyIdea("company", "🏪 What is a company?", {
        "3-5": ("Once upon a time, some friends loved making cookies. "
                "They baked and baked, and everybody smiled! "
                "A company is a team of friends making something wonderful together.",
                "If you and your friends started a team, what yummy thing would you make?"),
        "6-9": ("Imagine you and your best friends open a lemonade stand one sunny afternoon. "
                "You squeeze the lemons, your friend draws the signs, and customers line up! "
                "A company is just like that, only much bigger. "
                "It is a team of people working together to create something others enjoy.",
                "If you could build any company, what awesome thing would it create?"),
        "10-12": ("Picture a game studio where artists draw characters, coders build worlds "
                  "and testers play all day to find surprises. "
                  "Together they create something millions of players enjoy. "
                  "That is a company: a team turning an idea into something real. "
                  "The money players spend keeps the studio going and funds the next big project.",
                  "Which company makes something you love, and what do you think happens inside it every day?"),
        "13-17": ("A company is an organization that turns an idea into a product or service people pay for. "
                  "Think of the studio behind your favorite game or the streaming app you open most. "
                  "It started as someone's dream, grew into a team, and now earns money from millions of customers. "
                  "When revenue keeps flowing, the company can hire more people, build new products and grow. "
                  "Every stock listed on the market is a small piece of one of these businesses.",
                  "What makes you keep coming back to a company's product, and how do you think they keep it that way?"),
    }),
    # ------------------------------------------------------------------
    # 2. share
    # ------------------------------------------------------------------
    MoneyIdea("share", "🍕 A share is a tiny piece", {
        "3-5": ("Imagine a giant pizza cut into lots of slices. "
                "A company can be sliced up too! "
                "You can own one tiny slice.",
                "If you had a slice of a toy store, how cool would that be?"),
        "6-9": ("Imagine a giant pizza cut into hundreds of slices. "
                "A company can be split into tiny pieces like that, called shares. "
                "If you own one share, you own one small slice of the whole company! "
                "Lots of different people own the other slices, and together everyone shares the adventure.",
                "Which company would you love to own a tiny slice of?"),
        "10-12": ("Think of a trading card collection shared by your whole school. "
                  "Each card is one small piece of the collection. "
                  "A company works the same way: it is split into millions of tiny pieces called shares. "
                  "Owning one share means you hold a real piece of a real business. "
                  "When the company does great things, your little piece can become more valuable over time.",
                  "If you owned a piece of your favorite company, what would make you most excited to follow it?"),
        "13-17": ("A share, also called a stock, is a small unit of ownership in a company. "
                  "Big companies are split into millions or billions of shares so lots of people can own a piece. "
                  "Owning one does not let you run the company, but it connects you to how the business performs. "
                  "As the company grows and earns more, each share can become more valuable. "
                  "Shares let ordinary people become part-owners of the brands they already know and use.",
                  "Why might someone want to own a tiny piece of a company they already enjoy using?"),
    }),
    # ------------------------------------------------------------------
    # 3. stock_market
    # ------------------------------------------------------------------
    MoneyIdea("stock_market", "🎪 The stock market is a big marketplace", {
        "3-5": ("Think of a sunny market with colorful stalls everywhere. "
                "People come to swap their favorite things! "
                "Grown-ups have a special market where they swap tiny company pieces.",
                "What would you bring to a fun market?"),
        "6-9": ("Imagine the biggest school fair ever, with booths stretching as far as you can see. "
                "Instead of toys and games, people swap shares of companies. "
                "Someone who has a share can sell it to someone who wants one. "
                "Millions of swaps happen every day, all on computers!",
                "Why do you think so many people enjoy swapping shares?"),
        "10-12": ("Think of a huge online marketplace where people trade sneakers. "
                  "Buyers and sellers meet there and agree on a fair price for each pair. "
                  "The stock market works the same way, but people trade shares of companies. "
                  "It is open on weekdays, and computers match buyers with sellers in seconds. "
                  "That is why prices can change throughout the day as new people jump in.",
                  "What do you think makes the stock market exciting for so many people around the world?"),
        "13-17": ("Picture a massive online marketplace, kind of like a resale app, "
                  "but instead of sneakers or concert tickets, people trade tiny pieces of real businesses. "
                  "When you hit 'buy', a computer on the New York Stock Exchange matches you "
                  "with someone selling in a fraction of a second. "
                  "Every trade sets a fresh price, so the number keeps moving all day long. "
                  "The coolest part? Anyone can join in, not just big banks.",
                  "Why might a company want its shares traded on a stock market in the first place?"),
    }),
    # ------------------------------------------------------------------
    # 4. buyers_sellers
    # ------------------------------------------------------------------
    MoneyIdea("buyers_sellers", "🤝 Buyers and sellers set the price", {
        "3-5": ("Two friends both want a sparkly toy. "
                "One friend offers a bigger trade for it. "
                "Now the toy feels even more special! "
                "When more friends want it, it becomes more valuable.",
                "What is the most special thing you own?"),
        "6-9": ("Imagine you have a rare, sparkly trading card and three friends all want it. "
                "Each friend offers something a little better to win it. "
                "The card feels more and more valuable! "
                "Shares work the same way: when more people want to buy, the price goes up.",
                "What is the most amazing thing you have ever traded or collected?"),
        "10-12": ("Think about limited-edition sneakers on release day. "
                  "Lots of people want a pair and only a few exist, so people offer more to get them. "
                  "When fewer people are interested, sellers offer a friendlier deal. "
                  "A share price works the same way: it rises when more people want to buy "
                  "and dips a little when more people want to sell. "
                  "The crowd is always discovering the price together.",
                  "Can you think of something that became super popular and everyone wanted to get?"),
        "13-17": ("A share price is simply the most recent price a buyer and a seller agreed on. "
                  "When more people want to buy than sell, buyers offer higher and the price rises. "
                  "When more want to sell, sellers accept a bit less and the price dips. "
                  "Think of concert ticket resale prices jumping when a show sells out. "
                  "Nobody sets the price from above; the crowd of buyers and sellers discovers it together.",
                  "What do you think makes the crowd suddenly change its mind about what something is worth?"),
    }),
    # ------------------------------------------------------------------
    # 5. ups_and_downs [SMALL_DAY]
    # ------------------------------------------------------------------
    MoneyIdea("ups_and_downs", "🎵 Prices love to dance!", {
        "3-5": ("Waves at the beach go up and down all day. "
                "They never hold still! "
                "Prices dance up and down every day too. "
                "It is their happy little wiggle.",
                "Can you wiggle and dance like a little wave?"),
        "6-9": ("Think about the weather. "
                "Some days are a little warmer, some a little cooler, and that is totally normal. "
                "Share prices are like that too: they bounce up and down a little every single day. "
                "A small bounce is just the market's everyday rhythm, not a big deal at all.",
                "What else in your life changes a little bit every day?"),
        "10-12": ("Imagine you track your high score in a game you play every day. "
                  "Some rounds you crush it, some are just okay, even when you play the exact same way. "
                  "A share, which is a tiny piece of a company you can own, has a price that bounces like that too. "
                  "Every time someone buys or sells a share, the price nudges a tiny bit. "
                  "One wobbly day does not mean much; the adventure is in seeing where the price goes over weeks and months.",
                  "How would you tell the difference between a normal bounce and a real change?"),
        "13-17": ("Share prices move every day, often with no big news behind them at all. "
                  "Thousands of small trades, each with its own reason, add up to a little noise. "
                  "Think of your phone's battery estimate jumping around even when you use it the same way. "
                  "Day-to-day moves are mostly noise; the longer trend tells a richer story. "
                  "Learning to tell the two apart is a key skill for anyone interested in the market.",
                  "Why do you think people sometimes react strongly to small daily moves?"),
    }, tag=SMALL_DAY),
    # ------------------------------------------------------------------
    # 6. supply_demand
    # ------------------------------------------------------------------
    MoneyIdea("supply_demand", "⭐ When everyone wants something special", {
        "3-5": ("Everyone wants the last cookie on the plate. "
                "Hands reach from everywhere! "
                "When many people want the same thing, it feels extra special.",
                "What is something everyone always wants at your house?"),
        "6-9": ("When a brand-new toy comes out and every kid wants one, the shops run out fast! "
                "Some people offer extra to get theirs. "
                "That is called demand: how much people want something. "
                "When demand is big and only a few exist, the price goes up.",
                "What toy or game was super hard to get because everyone wanted one?"),
        "10-12": ("Imagine a new game console at launch. "
                  "Only a few exist and millions of people are eager to get one. "
                  "Supply is how much there is; demand is how much people want it. "
                  "When demand is bigger than supply, prices rise. "
                  "Shares follow the same rule: when more people want a piece of a company, the price climbs.",
                  "What do you think happens to the price when the store finally gets plenty of consoles?"),
        "13-17": ("Supply and demand explain most prices you encounter. "
                  "Supply is how much of something is available; demand is how much people want it. "
                  "Concert tickets, sneakers and shares all follow the same principle. "
                  "A company has a set number of shares, so when demand for them rises, the price rises too. "
                  "When demand eases, the price dips. "
                  "It is one of the simplest and most powerful ideas in all of economics.",
                  "What could make demand for a company's shares suddenly rise?"),
    }),
    # ------------------------------------------------------------------
    # 7. revenue
    # ------------------------------------------------------------------
    MoneyIdea("revenue", "🍋 Money coming in", {
        "3-5": ("You run a little lemonade stand. "
                "Friends give you shiny coins for cups of lemonade. "
                "All those coins coming in make you smile!",
                "How many cups of lemonade would you sell?"),
        "6-9": ("Imagine selling cups of lemonade for one coin each. "
                "If you sell twenty cups, you collect twenty coins! "
                "All the money a company collects from selling things is called revenue. "
                "Big companies collect revenue from millions of happy customers.",
                "What could you do to sell even more lemonade?"),
        "10-12": ("Think of a popular game that sells copies and in-game items. "
                  "Every single purchase adds money to the studio. "
                  "All of that incoming money is called revenue. "
                  "It is different from profit, because the studio still has bills to pay. "
                  "Revenue is like a scoreboard showing how much customers are buying.",
                  "Which companies do you think collect the most money from kids your age?"),
        "13-17": ("Revenue, also called sales, is all the money a company brings in from its customers. "
                  "For a streaming service, that means every subscription paid each month. "
                  "It is the top line of a company's financial report, before any costs are subtracted. "
                  "Growing revenue usually means more customers or higher prices. "
                  "But revenue alone does not tell you whether a company is making extra money, because costs matter too.",
                  "How could a company have lots of revenue coming in but still not be earning extra?"),
    }),
    # ------------------------------------------------------------------
    # 8. profit
    # ------------------------------------------------------------------
    MoneyIdea("profit", "✨ What you get to keep", {
        "3-5": ("Your stand earned ten coins today! "
                "Lemons cost six coins. "
                "Four shiny coins are all yours to keep! "
                "That is the best part.",
                "What would you do with your extra coins?"),
        "6-9": ("Say your lemonade stand brings in ten coins. "
                "But lemons, sugar and cups cost you six coins. "
                "The four coins left over are your profit: the part you truly get to keep! "
                "Companies work the same way: profit is what remains after paying for everything.",
                "How could your lemonade stand keep even more coins at the end of the day?"),
        "10-12": ("Imagine you make and sell colorful friendship bracelets. "
                  "You earn money from each one, but beads and string cost money too. "
                  "Profit is what you keep after paying for all your supplies. "
                  "Companies try to grow profit by selling more or finding smarter ways to work. "
                  "Profit is one big reason people get excited about owning a piece of a company.",
                  "If your bracelets became super popular, what clever ideas would help you keep more profit?"),
        "13-17": ("Profit is what remains after a company pays all its costs: workers, materials, rent, technology and taxes. "
                  "Think of a part-time job: what you earn is revenue, and what you keep after your own spending is profit. "
                  "Companies that earn steady profits can invest in new ideas or reward their owners. "
                  "Some companies choose to spend more now, building toward even bigger possibilities later.",
                  "Why might a company choose to earn smaller profits now in order to grow?"),
    }),
    # ------------------------------------------------------------------
    # 9. news [BIG_MOVE]
    # ------------------------------------------------------------------
    MoneyIdea("news", "📣 Exciting news travels fast!", {
        "3-5": ("A new ice cream shop opens down the street! "
                "Everyone hears how yummy it is. "
                "Lots of people rush over to try it! "
                "Exciting news makes everyone want to join in.",
                "What exciting news would make you want to run down the street?"),
        "6-9": ("Imagine hearing that a toy company just invented the most amazing toy ever. "
                "Suddenly everyone wants a piece of that company! "
                "Big news travels fast and changes how people feel. "
                "When news is exciting, more people want to join in, and prices go up.",
                "What amazing news about a company would get you the most excited?"),
        "10-12": ("Think of a game trailer that goes viral overnight. "
                  "Suddenly the whole internet is buzzing about the studio. "
                  "News works exactly like that for companies. "
                  "Exciting news can make people eager to buy shares, so the price rises. "
                  "Surprising news can make people pause to think, so the price dips a little. "
                  "Markets react fast, often within seconds of a big announcement.",
                  "Can you think of a time exciting news changed how everyone felt about something?"),
        "13-17": ("Prices react to new information. "
                  "A product launch, a partnership, an earnings report or a major announcement "
                  "can change what people think a company is worth. "
                  "Markets process news incredibly fast, often within seconds. "
                  "Sometimes the initial reaction is bigger than the news itself, and later the price settles. "
                  "That is why understanding the story behind a move matters more than the move alone.",
                  "Why might a price jump on exciting news and then gradually settle back?"),
    }, tag=BIG_MOVE),
    # ------------------------------------------------------------------
    # 10. market_together [MARKET_DAY]
    # ------------------------------------------------------------------
    MoneyIdea("market_together", "⛵ When all the boats float together", {
        "3-5": ("When the ocean rises, all the boats float higher together. "
                "When it dips, they all dip together. "
                "Sometimes all the shops move the same way, just like boats!",
                "Can you think of something that changes for everyone at the same time?"),
        "6-9": ("When it rains at a big outdoor party, everybody gets a little wet, not just one family. "
                "Sometimes big news affects almost every company at once. "
                "Then lots of prices move in the same direction on the same day. "
                "It does not mean any one company did something special; the whole market just floated together!",
                "What is something that can change everyone's day at the same time?"),
        "10-12": ("Think about a snow day: every school in town is off, not just yours. "
                  "Some news is like that for companies, such as news about the whole economy. "
                  "When it happens, most prices move the same way on the same day. "
                  "So a stock can dip even if the company itself had no news at all. "
                  "Comparing one stock with the whole market helps you see the bigger picture.",
                  "How could you figure out if a stock moved because of its own news or because of everything?"),
        "13-17": ("Some days, nearly every stock moves in the same direction. "
                  "That is usually driven by market-wide news such as interest rates, employment data or inflation. "
                  "It is like a rising tide lifting every boat, or an ebbing tide lowering them gently. "
                  "Comparing a single stock with the S&P 500 reveals whether the move was about that company "
                  "or about the whole market. "
                  "This kind of shared movement is something every investor experiences.",
                  "Why would news about interest rates affect companies in completely different industries?"),
    }, tag=MARKET_DAY),
    # ------------------------------------------------------------------
    # 11. hopes_worries [BIG_MOVE]
    # ------------------------------------------------------------------
    MoneyIdea("hopes_worries", "🌈 Dreams about tomorrow", {
        "3-5": ("You hope tomorrow is sunny and full of fun. "
                "That makes you happy right now! "
                "Grown-ups have hopes and dreams about companies too.",
                "What exciting thing are you hoping for tomorrow?"),
        "6-9": ("Imagine everyone is buzzing about a brand-new playground opening next week. "
                "Before it even opens, kids are already thrilled! "
                "Prices work like that too: they can move based on what people hope will happen next. "
                "Nobody knows the future for sure, and that is what makes it all so interesting.",
                "What is something you were really excited about before it even happened?"),
        "10-12": ("Think of a sports team that trades for a superstar player. "
                  "Fans are thrilled before a single game is played! "
                  "Share prices often move on hopes and dreams like that, not just on what already happened. "
                  "When people feel optimistic, they offer more for shares. "
                  "When their excitement cools, the price may ease back a little. "
                  "That is why prices can shift before anything actually changes.",
                  "Have you ever been so excited about something that the waiting felt as fun as the thing itself?"),
        "13-17": ("Share prices reflect what people think a company will earn in the future, not only what it has done so far. "
                  "Think of the buzz before a major product launch: excitement builds long before anyone holds the product. "
                  "When optimism grows, people pay more for shares; when it fades, they offer less. "
                  "That is why a price can dip even after good news, if people had already been expecting great news. "
                  "Nobody knows the future, so prices keep adjusting as new information arrives.",
                  "Why could a company announce good results and still see its share price dip slightly?"),
    }, tag=BIG_MOVE),
    # ------------------------------------------------------------------
    # 12. earnings
    # ------------------------------------------------------------------
    MoneyIdea("earnings", "📋 The company's report card", {
        "3-5": ("At school you earn gold stars for a great job! "
                "Stars show how well you are doing. "
                "Companies get a kind of report card too.",
                "What would your gold star be for today?"),
        "6-9": ("At school, a report card shows how you did in each subject. "
                "Every few months, companies share a report card too! "
                "It shows how much they sold and how much they earned. "
                "People love reading it to see if the company is doing well and getting even better.",
                "If a company had a report card, what subjects would be on it?"),
        "10-12": ("Every few months, big companies share a report, much like a school report card. "
                  "It is called an earnings report. "
                  "It shows how much money came in, how much was spent and how much the company kept. "
                  "People compare it with what they expected beforehand. "
                  "A pleasant surprise can lift the price, while meeting what people hoped keeps things steady.",
                  "Why do you think a surprise on a report card creates more excitement than an expected result?"),
        "13-17": ("Public companies publish earnings reports every quarter, meaning every three months. "
                  "They show revenue, costs, profit and often guidance, which is what the company expects for the future. "
                  "Analysts publish estimates beforehand, so the market reacts to the gap between actual results and those estimates. "
                  "Beating estimates can lift a price; meeting them keeps things steady. "
                  "Guidance often matters as much as the results, because it shows where the company sees itself heading.",
                  "Why might what a company says about next year carry as much weight as last quarter's actual results?"),
    }),
    # ------------------------------------------------------------------
    # 13. index [MARKET_DAY]
    # ------------------------------------------------------------------
    MoneyIdea("index", "🧺 A basket of many companies", {
        "3-5": ("A fruit basket holds apples, bananas, grapes and pears. "
                "Some special baskets hold lots of company pieces! "
                "One basket can hold hundreds of them.",
                "What yummy things would you put in your fruit basket?"),
        "6-9": ("Imagine a giant basket holding a tiny piece of 500 different companies. "
                "That basket is called the S&P 500. "
                "If most pieces in the basket go up, the whole basket goes up! "
                "It is a quick way to see how most big companies did today.",
                "If you filled a basket with your favorite companies, which ones would you pick?"),
        "10-12": ("Imagine a playlist with the 500 most popular songs. "
                  "Its overall rating shows how music is doing in general, not just one song. "
                  "The S&P 500 is like that for companies: it tracks 500 big ones together. "
                  "People use it to see how the whole market did today. "
                  "Your recap compares each stock with it, so you can tell if one stood out or just moved with everyone.",
                  "Why is it helpful to compare one stock with the whole basket?"),
        "13-17": ("An index tracks a group of stocks to show how a slice of the market is doing. "
                  "The S&P 500 follows about 500 large US companies, weighted by size. "
                  "Funds that copy an index let people own a slice of all of them at once, "
                  "which is one of the simplest ways to participate in the market. "
                  "Comparing one stock with the index shows whether it outperformed or followed the broader trend. "
                  "That is exactly how your recap's S&P 500 line works.",
                  "Why might a single stock rise on a day the overall index dips?"),
    }, tag=MARKET_DAY),
    # ------------------------------------------------------------------
    # 14. long_term
    # ------------------------------------------------------------------
    MoneyIdea("long_term", "🌱 Growing takes time", {
        "3-5": ("You plant a tiny seed in the dirt. "
                "You water it every day. "
                "Slowly, slowly, it grows into a big, beautiful plant!",
                "What would you love to watch grow?"),
        "6-9": ("Planting a seed takes patience. "
                "Some days you can not even see it grow. "
                "But week after week, it becomes taller, until one day it is a beautiful big plant! "
                "Companies can grow like that too, bit by bit over many years, even with quiet days in between.",
                "What is something that took a really long time but turned out wonderfully?"),
        "10-12": ("Think about learning an instrument or a sport. "
                  "Some practice days go amazingly, and some feel like you stayed in the same place. "
                  "But when you look back after a year, you have gotten so much better! "
                  "Companies are similar: they can have quiet days and still grow over years. "
                  "Looking at the long journey shows more than any single day ever could.",
                  "What skill have you gotten better at over time, even when some days felt slow?"),
        "13-17": ("Over long periods, a company's value tends to follow its business performance, "
                  "but day to day it follows the crowd's mood. "
                  "Think of training for a season: one quiet game does not define the year. "
                  "Many investors think in years, not days, because short-term moves are unpredictable "
                  "while long-term trends are shaped more by the actual business. "
                  "Time gives a fuller picture than any single headline.",
                  "Why do you think short-term moves are harder to predict than long-term trends?"),
    }),
    # ------------------------------------------------------------------
    # 15. saving
    # ------------------------------------------------------------------
    MoneyIdea("saving", "🐷 Saving for something amazing", {
        "3-5": ("You have three cookies. "
                "You eat one yummy one now. "
                "You save two for a special day! "
                "Saving is keeping treats for later.",
                "What special treat would you save for later?"),
        "6-9": ("Imagine putting a few coins into your piggy bank every week. "
                "It might feel slow at first, but little by little, your piggy bank gets heavier! "
                "Saving means keeping some money now so you can use it for something amazing later. "
                "It gives you the power to get bigger things you could not buy all at once.",
                "What amazing thing would you save up for in your piggy bank?"),
        "10-12": ("Say you want a game that costs way more than one week's allowance. "
                  "If you set a little aside every week, you will get there! "
                  "Saving means choosing to keep money for something special in the future "
                  "instead of spending it all today. "
                  "It takes patience, but it gives you more choices and more possibilities. "
                  "Grown-ups save for big dreams too.",
                  "What would you be willing to save for, and how long would it take?"),
        "13-17": ("Saving means setting money aside instead of spending it all now. "
                  "Think of putting part of each paycheck from a job toward a car, a trip or a future goal. "
                  "Savings give you options and a cushion for the unexpected. "
                  "Some people keep savings in a bank; some invest part of them, which adds excitement and potential. "
                  "The habit of saving matters more than the amount when you are starting out.",
                  "What would make saving feel easier for you: a specific goal, a plan or something else?"),
    }),
    # ------------------------------------------------------------------
    # 16. compound
    # ------------------------------------------------------------------
    MoneyIdea("compound", "⛄ The snowball that grows and grows", {
        "3-5": ("You roll a tiny snowball across the snow. "
                "It picks up more and more! "
                "It grows bigger and bigger and bigger. Hooray!",
                "How big do you think your snowball could get?"),
        "6-9": ("Picture rolling a small snowball down a snowy hill. "
                "It picks up snow, and the bigger it gets, the more it picks up! "
                "Money can grow like that when your earnings earn even more on top. "
                "It is called compounding, and it works best with lots of time.",
                "Why do you think a bigger snowball picks up even more snow?"),
        "10-12": ("Imagine a fun video going viral. "
                  "Each person who watches it shares it, and each share brings in more viewers who share it again. "
                  "The views grow faster and faster! "
                  "Money can grow a bit like that when earnings get added back in and then earn more themselves. "
                  "This is called compounding, and the longer it runs, the more powerful it becomes.",
                  "Why does starting the snowball early make such a big difference?"),
        "13-17": ("Compounding means earning on your past earnings, not just on what you originally put in. "
                  "Think of a channel where each new subscriber helps attract even more subscribers. "
                  "Over many years, this growth-on-growth can add up to far more than steady, flat growth would. "
                  "Time is the key ingredient, which is why starting early is so powerful. "
                  "Understanding compounding is one of the most valuable financial insights you can have at any age.",
                  "How could compounding work in your favor if you started saving at your age?"),
    }),
    # ------------------------------------------------------------------
    # 17. risk
    # ------------------------------------------------------------------
    MoneyIdea("risk", "🗺️ Every adventure has surprises", {
        "3-5": ("You go on a treasure hunt in the garden! "
                "Sometimes you find sparkly gems. "
                "Sometimes you find funny-shaped rocks. "
                "Every surprise makes the adventure exciting!",
                "What is the coolest treasure you have ever found?"),
        "6-9": ("Think about climbing a really tall tree. "
                "The higher you go, the more amazing the view gets! "
                "But you also take more careful steps along the way. "
                "Owning shares is a bit like that: the exciting part and the uncertain part come together.",
                "Would you rather climb a tall tree with an amazing view or a short one that is super easy?"),
        "10-12": ("Think of learning a new trick on a skateboard or bike. "
                  "There is a chance it works out great on the first try, and a chance you need a few more attempts. "
                  "That is what adventure means: the outcome might surprise you. "
                  "Shares carry that same kind of excitement: their price can go up or dip back a little. "
                  "People who own shares embrace that uncertainty in exchange for the chance to grow over time.",
                  "What is an adventure you tried where you were not sure how it would turn out?"),
        "13-17": ("Risk means the outcome may be different from what you expected. "
                  "Shares can change in value in either direction, and individual companies go through ups and downs. "
                  "Generally, opportunities with higher growth potential also carry greater uncertainty. "
                  "Think of choosing between an easy class and a challenging one: "
                  "the harder class offers more to gain, but the journey is less predictable. "
                  "Understanding risk helps you make thoughtful choices, which is one of the most valuable skills in investing.",
                  "Why do you think higher potential rewards usually come paired with more uncertainty?"),
    }),
    # ------------------------------------------------------------------
    # 18. spreading_out
    # ------------------------------------------------------------------
    MoneyIdea("spreading_out", "🌻 Planting many different seeds", {
        "3-5": ("You plant seeds in many little pots. "
                "Some grow red flowers! "
                "Some grow tall sunflowers! "
                "Your garden is full of colorful surprises.",
                "How many colors would your dream garden have?"),
        "6-9": ("Imagine you have a garden with just one plant. "
                "If that one plant has a sleepy week, your garden looks empty. "
                "But with ten different plants, some are always blooming! "
                "Some people own pieces of many companies for the same reason. "
                "One quiet day for one company does not change the whole picture.",
                "What different plants would you put in your dream garden?"),
        "10-12": ("Think of a band with many talented musicians. "
                  "If one player takes a day off, the music still sounds wonderful because the others carry on. "
                  "Some people own pieces of many different companies for the same reason. "
                  "This is called diversifying: when one company has a quiet stretch, "
                  "the others can keep things moving forward. "
                  "It is like having a team where everyone contributes.",
                  "Why might a band with many great musicians have more consistent performances?"),
        "13-17": ("Diversification means spreading your money across many different investments instead of one. "
                  "If one company has a quiet year, others may balance things out. "
                  "Think of a playlist with dozens of artists: one album that does not land as well "
                  "hardly changes your overall experience. "
                  "Index funds are one popular way people diversify. "
                  "It reduces the impact of any single company, though it does not protect against days "
                  "when the whole market moves together.",
                  "Why does spreading investments across many companies reduce the impact from any one of them?"),
    }),
    # ------------------------------------------------------------------
    # 19. needs_wants
    # ------------------------------------------------------------------
    MoneyIdea("needs_wants", "💭 Treasures and treats", {
        "3-5": ("We need yummy food and a cozy bed. "
                "Those are our treasures! "
                "We dream about toys and treats too. "
                "Dreams are fun, but treasures come first!",
                "Is a teddy bear a treasure or a treat?"),
        "6-9": ("Needs are the things you truly must have, like food, warm clothes and a home. "
                "Wants are the fun extras, like a new game or a cool toy. "
                "Families take care of needs first, then pick their favorite wants. "
                "Companies sell both: some make everyday needs, some make exciting wants!",
                "Can you name one need and one fun want you have today?"),
        "10-12": ("Think about a new phone versus school shoes. "
                  "You need the shoes; the newest phone is more of a want. "
                  "Knowing the difference helps you make great choices with your money. "
                  "Some companies sell everyday needs, like groceries, and some sell exciting extras, like the latest gadgets. "
                  "Companies that sell everyday needs tend to have steadier days on the stock market.",
                  "Why might companies that sell everyday needs have steadier days than ones that sell exciting extras?"),
        "13-17": ("Needs are essentials like food, housing and transportation; wants are the extras that make life fun. "
                  "Budgeting means covering your needs first and choosing your wants with intention. "
                  "Companies feel this distinction too: people keep buying groceries in any economy "
                  "but may hold off on a new gadget until the timing feels right. "
                  "That is one reason some companies have more price movement than others. "
                  "Recognizing needs and wants in your own spending is one of the most practical financial skills.",
                  "Which of your regular purchases would you keep first if you had to choose?"),
    }),
    # ------------------------------------------------------------------
    # 20. choices
    # ------------------------------------------------------------------
    MoneyIdea("choices", "🔀 Choosing your adventure", {
        "3-5": ("At the playground you can try the swing or the slide. "
                "One more turn! "
                "You pick the swing today. "
                "The slide will be waiting for you tomorrow!",
                "Swing or slide: which one would you pick today?"),
        "6-9": ("Imagine you have just enough to buy either a book or a toy, but not both right now. "
                "If you pick the book, the toy waits for another day. "
                "Every choice means picking one adventure and saving another for later. "
                "Grown-ups and companies make choices like this all the time!",
                "What was a fun choice you had to make between two great things?"),
        "10-12": ("Say you can spend Saturday at a friend's party or at a team game, but not both. "
                  "Choosing one means saving the other adventure for another time. "
                  "What you save for later is called the opportunity cost. "
                  "Money works the same way: spending on one thing means those funds are not available "
                  "for something else right now. "
                  "Companies weigh this too, deciding between building new products or improving what they already have.",
                  "What was the opportunity cost of the last fun thing you chose to do?"),
        "13-17": ("Opportunity cost is the value of what you set aside when you choose something else. "
                  "Spending money today means those funds are not saved or invested for later. "
                  "Companies face this too: spending heavily on one project means fewer resources for others. "
                  "That is often what markets discuss when a company announces a big new initiative. "
                  "Thoughtful decision-making means weighing what you gain against what you are setting aside.",
                  "When a company invests billions in a new idea, what opportunities might it be setting aside?"),
    }),
]

_BY_KEY = {idea.key: idea for idea in CURRICULUM}


def get(key: str) -> MoneyIdea | None:
    return _BY_KEY.get(key)


def position(idea: MoneyIdea) -> int:
    """1-based place in the curriculum."""
    return CURRICULUM.index(idea) + 1


def next_idea(db: DBSession, user_id: int, tags: set[str]) -> MoneyIdea:
    """The next idea for this user: basics first, then one that fits the
    day if any is left, else the next in order. After the whole curriculum,
    it starts again from the idea sent longest ago (preferring a fitting
    one among the five oldest)."""
    last_sent = dict(db.execute(
        select(MoneyIdeaSend.idea_key, func.max(MoneyIdeaSend.trading_date))
        .where(MoneyIdeaSend.user_id == user_id)
        .group_by(MoneyIdeaSend.idea_key)
    ).all())
    unsent = [i for i in CURRICULUM if i.key not in last_sent]
    if unsent:
        if len(last_sent) >= FOUNDATION:
            fitting = [i for i in unsent if i.tag in tags]
            if fitting:
                return fitting[0]
        return unsent[0]
    oldest = sorted(CURRICULUM, key=lambda i: last_sent[i.key])[:5]
    return next((i for i in oldest if i.tag in tags), oldest[0])


def record(db: DBSession, user_id: int, idea: MoneyIdea, day: date) -> None:
    """Remember what this user got today (caller commits)."""
    db.add(MoneyIdeaSend(user_id=user_id, idea_key=idea.key, trading_date=day))


# ---------------------------------------------------------------------------
# Email block
# ---------------------------------------------------------------------------


def _chosen(idea: MoneyIdea, age_groups: list[str]) -> list[tuple[str, str, str]]:
    """(age label, idea, question) for the parent's age groups."""
    from app.services.articles.render import AGE_LABELS

    groups = [g for g in age_groups if g in idea.versions] or ["6-9"]
    return [(AGE_LABELS[g], *idea.versions[g]) for g in groups]


def _heading(idea: MoneyIdea) -> str:
    return f"MONEY IDEA OF THE DAY · {position(idea)} of {len(CURRICULUM)}"


def idea_text(
    idea: MoneyIdea, age_groups: list[str], history_url: str,
    feedback_text: str | None = None,
) -> str:
    lines = [f"\U0001f331 {_heading(idea)}", idea.title]
    for label, text, question in _chosen(idea, age_groups):
        lines += ["", label, text, f"\U0001f5e3️ Ask your kid: {question}"]
    if feedback_text:
        lines += ["", feedback_text]
    lines += ["", f"Every money idea you've had so far: {history_url}"]
    return "\n".join(lines)


def idea_html(
    idea: MoneyIdea, age_groups: list[str], history_url: str,
    feedback_html: str | None = None,
) -> str:
    muted = "#6e7781"
    kids = "".join(
        f"""\
<div style="border-top:1px solid #d8eedd;margin-top:12px;padding-top:10px">
<p style="margin:0;font-size:13px;color:{muted}">{escape(label)}</p>
<p style="font-size:16px;line-height:1.5;margin:4px 0 8px">{escape(text)}</p>
<p style="background:#fff8c5;border-radius:8px;padding:10px;margin:0">\U0001f5e3️ <strong>Ask your kid:</strong> {escape(question)}</p>
</div>"""
        for label, text, question in _chosen(idea, age_groups)
    )
    fb = feedback_html or ""
    return f"""\
<div style="border:1px solid #b7e1c1;background:#f3fbf5;border-radius:10px;padding:16px;margin:16px 0">
<p style="margin:0 0 4px;font-size:12px;letter-spacing:.04em;color:{muted}">\U0001f331 {escape(_heading(idea))}</p>
<h2 style="margin:0;font-size:19px">{escape(idea.title)}</h2>
{kids}
{fb}
<p style="margin:12px 0 0;font-size:13px"><a href="{escape(history_url, quote=True)}">Every money idea you've had so far ↗</a></p>
</div>"""
